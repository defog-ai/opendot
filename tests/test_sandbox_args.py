from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conftest import FakeDocker
from opendot.config import Config
from opendot.container_contract import (
    REQUIRED_DIRECTORIES,
    REQUIRED_EXECUTABLES,
    build_image_args,
    check_verify_output,
    dockerfile_path,
    verify_image_args,
)
from opendot.models import Mount, Step
from opendot.sandbox import (
    CONTAINER_HOME,
    SandboxError,
    adopt_session,
    allowlisted_env,
    check_mounts,
    check_step_env,
    configured_mounts,
    docker_run_args,
    new_session,
    open_session,
    stop_container,
)


def make_config(tmp_path: Path, **sandbox: object) -> Config:
    cfg = Config.from_dict(
        {
            "core": {"state_root": str(tmp_path / "state")},
            "backend": {"codex": {"auth_file": str(tmp_path / "codex" / "auth.json")}},
            "sandbox": sandbox,
        },
        env={},
    )
    cfg.ensure_directories()
    return cfg


def run_args(cfg: Config, *, step: Step = Step.WORK, **kwargs: object) -> list[str]:
    session = new_session(cfg, "codex")
    return docker_run_args(
        cfg.sandbox,
        name="opendot-test",
        command=["codex", "app-server"],
        session=session,
        work_writable=step is not Step.REVIEW,
        **kwargs,
    )


def pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, value in enumerate(args[:-1]) if value == flag]


def test_hardening_flags(tmp_path: Path) -> None:
    args = run_args(make_config(tmp_path))
    assert args[:3] == ["docker", "run", "--rm"]
    assert pairs(args, "--cap-drop") == ["ALL"]
    assert pairs(args, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in args
    user = pairs(args, "--user")[0]
    assert user.split(":")[0] != "0"
    assert pairs(args, "--pids-limit") == ["512"]
    assert pairs(args, "--memory") == ["4g"]
    assert pairs(args, "--cpus") == ["2"]
    tmpfs = pairs(args, "--tmpfs")
    assert any(t.startswith("/tmp:") for t in tmpfs)
    assert any(t.startswith(f"{CONTAINER_HOME}:") for t in tmpfs)
    assert f"HOME={CONTAINER_HOME}" in pairs(args, "--env")
    assert "--privileged" not in args
    assert not any("docker.sock" in a for a in args)


def test_network_comes_from_config(tmp_path: Path) -> None:
    args = run_args(make_config(tmp_path, network="none"))
    assert pairs(args, "--network") == ["none"]


def test_host_network_is_refused(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    for network in ("host", "", "container:other"):
        sandbox = dataclasses.replace(cfg.sandbox, network=network)
        with pytest.raises(SandboxError):
            docker_run_args(
                sandbox,
                name="opendot-test",
                command=["true"],
                session=new_session(cfg, "codex"),
                work_writable=True,
            )


def test_work_mount_is_read_only_for_review(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    review = pairs(run_args(cfg, step=Step.REVIEW), "--mount")
    work = pairs(run_args(cfg, step=Step.WORK), "--mount")
    assert any("dst=/work" in m and m.endswith(",readonly") for m in review)
    assert any("dst=/work" in m and not m.endswith(",readonly") for m in work)


def test_env_names_are_bare(tmp_path: Path) -> None:
    args = run_args(make_config(tmp_path), env_names=["MY_TOKEN"])
    assert "MY_TOKEN" in pairs(args, "--env")
    assert not any(a.startswith("MY_TOKEN=") for a in args)


def test_image_and_command_come_last(tmp_path: Path) -> None:
    args = run_args(make_config(tmp_path, image="example/step:1"))
    assert args[-3:] == ["example/step:1", "codex", "app-server"]


@pytest.mark.parametrize(
    "host", ["/var/run/docker.sock", "/var/run", "/run/docker.sock", "/run", "/var", "/"]
)
def test_docker_socket_is_refused(tmp_path: Path, host: str) -> None:
    cfg = make_config(tmp_path)
    with pytest.raises(SandboxError):
        check_mounts(Step.WORK, [Mount(Path(host), "/data")], cfg)


def test_symlink_to_docker_socket_folder_is_refused(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    link = tmp_path / "sock-link"
    link.symlink_to("/var/run")
    with pytest.raises(SandboxError):
        check_mounts(Step.WORK, [Mount(link, "/data")], cfg)


def test_state_root_and_login_file_are_refused(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    auth = tmp_path / "codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}")
    inside = cfg.state_root / "sessions"
    inside.mkdir(exist_ok=True)
    for host in (cfg.state_root, inside, tmp_path, auth, auth.parent):
        with pytest.raises(SandboxError):
            check_mounts(Step.WORK, [Mount(host, "/data")], cfg)


@pytest.mark.parametrize(
    "container",
    ["data", "/work", "/work/sub", "/tmp/x", CONTAINER_HOME, "/opendot", "/", "/a/../b"],
)
def test_bad_container_paths_are_refused(tmp_path: Path, container: str) -> None:
    cfg = make_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(SandboxError):
        check_mounts(Step.WORK, [Mount(data, container)], cfg)


def test_mount_checks(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    good = [Mount(data, "/data")]
    assert check_mounts(Step.WORK, good, cfg) == good
    with pytest.raises(SandboxError):
        check_mounts(Step.REVIEW, good, cfg)
    with pytest.raises(SandboxError):
        check_mounts(Step.WORK, [Mount(tmp_path / "missing", "/data")], cfg)
    with pytest.raises(SandboxError):
        check_mounts(Step.WORK, [Mount(data, "/data"), Mount(data, "/data")], cfg)
    with pytest.raises(SandboxError):
        check_mounts(Step.WORK, [Mount(tmp_path / "a,b", "/data")], cfg)


def test_operator_mounts_are_always_read_only(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    args = run_args(cfg, mounts=[Mount(data, "/data", read_only=False)])
    spec = [m for m in pairs(args, "--mount") if "dst=/data" in m]
    assert spec and spec[0].endswith(",readonly")


def test_configured_mounts_are_read_only(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    cfg = make_config(tmp_path, readonly_mounts=[{"host": str(data), "container": "/data"}])
    mounts = configured_mounts(cfg)
    assert [(m.container, m.read_only) for m in mounts] == [("/data", True)]


def test_env_allowlist(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.sandbox.env_allowlist == []
    assert allowlisted_env(cfg, {"PATH": "/bin", "SECRET_THING": "x" * 20}) == {}
    with pytest.raises(SandboxError):
        check_step_env(Step.WORK, {"SECRET_THING": "value"}, cfg)

    cfg = make_config(tmp_path, env_allowlist=["LANG_CHOICE"])
    assert allowlisted_env(cfg, {"LANG_CHOICE": "en", "OTHER": "x"}) == {"LANG_CHOICE": "en"}
    assert check_step_env(Step.WORK, {"LANG_CHOICE": "en"}, cfg) == {"LANG_CHOICE": "en"}
    with pytest.raises(SandboxError):
        check_step_env(Step.REVIEW, {"LANG_CHOICE": "en"}, cfg)


@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENDOT_SLACK_BOT_TOKEN",
        "HOME",
        "PATH",
    ],
)
def test_reserved_env_names_are_refused_even_if_allowlisted(tmp_path: Path, name: str) -> None:
    # The config loader refuses the login variables; this builds a config around it.
    cfg = make_config(tmp_path)
    cfg = dataclasses.replace(cfg, sandbox=dataclasses.replace(cfg.sandbox, env_allowlist=[name]))
    with pytest.raises(SandboxError):
        check_step_env(Step.WORK, {name: "value"}, cfg)


def test_sessions(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    session = new_session(cfg, "codex")
    assert session.cli.is_dir() and session.work.is_dir()
    adopted = adopt_session(session, cfg, "codex", "thread-1")
    assert adopted.root.name == "thread-1" and not session.root.exists()
    assert open_session(cfg, "codex", "thread-1") == adopted
    with pytest.raises(SandboxError):
        open_session(cfg, "codex", "../escape")
    with pytest.raises(SandboxError):
        open_session(cfg, "codex", "missing")


def test_stop_container_runs_docker_kill() -> None:
    docker = FakeDocker()
    stop_container(docker, "docker", "opendot-x")
    assert docker.runs[0].args == ["docker", "kill", "opendot-x"]


def test_image_build_and_check_commands() -> None:
    assert build_image_args("docker", "img:1") == [
        "docker",
        "build",
        "--pull",
        "--tag",
        "img:1",
        "-",
    ]
    text = dockerfile_path().read_text()
    assert "COPY --from=" in text and "\nCOPY ." not in text and "ADD " not in text
    assert "docker.sock" not in text
    args = verify_image_args("docker", "img:1")
    assert pairs(args, "--network") == ["none"]
    good = ["uid=10001"]
    good += [f"tool {t} /usr/bin/{t}" for t in REQUIRED_EXECUTABLES]
    good += [f"dir {d} ok" for d in REQUIRED_DIRECTORIES]
    assert check_verify_output(0, "\n".join(good)).ok
    bad = check_verify_output(0, "uid=0\ntool codex missing\n")
    assert not bad.ok
    assert any("root" in p for p in bad.problems)
    assert any("codex is not installed" in p for p in bad.problems)


def test_container_never_runs_as_root(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    with pytest.raises(SandboxError):
        run_args(cfg, user=(0, 0))
    args = run_args(cfg, user=(1234, 1234))
    assert pairs(args, "--user") == ["1234:1234"]
    home = [t for t in pairs(args, "--tmpfs") if t.startswith(f"{CONTAINER_HOME}:")]
    assert "uid=1234,gid=1234" in home[0]
