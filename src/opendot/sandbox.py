"""The Docker container that every model step runs in.

Each step runs in a fresh container started with `docker run --rm`. The container:
- drops every Linux capability and, unless sandbox.no_new_privileges is false
  (needed for Docker installed as a snap), cannot gain new privileges;
- has a read-only root file system, with tmpfs folders at /tmp and at HOME;
- runs as a non-root user (the host user's ids, or a fixed user when that is root);
- has CPU, memory and process limits;
- uses the Docker network named in the config ("host" and "container:..." are refused);
- never gets the Docker socket, the state root or the host's login files as mounts.

Two host folders per backend session are mounted: `work/` at /work (read-only in
review steps) and `cli/` at /opendot/cli, where the CLI keeps its session files so
a later step can resume the same session. They live under
state_root/sessions/<backend kind>/<session id>/.

Work steps may also get host mounts (v0.2): folders the host made under the
state root for this step, each on one fixed container path under /opendot (see
check_host_mounts). A task's copy of a repository is writable, but its .git
folder is mounted read-only on top, so only the host can commit.

Secret values never appear in the argument list. A variable is passed as
`--env NAME` and the docker client copies its value from its own environment,
which the backend sets when it starts the process.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from opendot.models import HostMount, HostMountKind, McpServerSpec, Mount, Step

if TYPE_CHECKING:
    from opendot.backends import CommandRunner
    from opendot.config import Config, SandboxConfig

# The user baked into the image. Step containers run as the host user instead
# (see container_user); this one is used only when the host user is root.
CONTAINER_UID = 10001
CONTAINER_GID = 10001
CONTAINER_HOME = "/opendot/home"
CONTAINER_WORKDIR = "/work"
CONTAINER_CLI_HOME = "/opendot/cli"

# Fixed container places for host mounts (v0.2).
CONTAINER_REPOS = "/opendot/repos"  # /opendot/repos/<repository name>
CONTAINER_ARTIFACTS = "/opendot/run/artifacts"
CONTAINER_MCP = "/opendot/mcp"
CONTAINER_INSTRUCTIONS = "/opendot/instructions"  # /opendot/instructions/<connector name>

# Paths inside the container that OpenDot manages. Operator mounts may not land
# on them, under them, or above them.
RESERVED_CONTAINER_PATHS = ("/tmp", CONTAINER_HOME, CONTAINER_WORKDIR, "/opendot", "/proc", "/dev")

DOCKER_SOCKETS = (Path("/var/run/docker.sock"), Path("/run/docker.sock"))

# Variables a caller may never pass through env: they would override the sandbox
# layout or hand a backend login to a step that did not choose that backend.
RESERVED_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "SHELL",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "DOCKER_HOST",
    }
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
_UNSAFE_MOUNT_CHARS = (",", '"', "\n", "\r", "\0")


class SandboxError(ValueError):
    """A request that the sandbox refuses: a forbidden mount, variable or session id."""


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def allowlisted_env(config: Config, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The host variables named in sandbox.env_allowlist that are set. For work steps."""
    source = os.environ if source is None else source
    return {name: source[name] for name in config.sandbox.env_allowlist if name in source}


def check_step_env(step: Step, env: Mapping[str, str], config: Config) -> dict[str, str]:
    """Validate the variables a caller wants in a step container and return a copy.

    Review steps get none. Every name must be in sandbox.env_allowlist and must not
    be a reserved name or a host variable that holds a login (Config.secret_env_names).
    """
    env = dict(env)
    if Step(step) is Step.REVIEW and env:
        raise SandboxError("review steps run without environment variables")
    allowed = set(config.sandbox.env_allowlist)
    host_secrets = config.secret_env_names()
    for name in env:
        if not _ENV_NAME.match(name):
            raise SandboxError(f"{name!r} is not a valid variable name")
        if name in RESERVED_ENV_NAMES or name in host_secrets:
            raise SandboxError(f"{name} is reserved and cannot be passed into a step")
        if name not in allowed:
            raise SandboxError(f"{name} is not in sandbox.env_allowlist")
    return env


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------


def configured_mounts(config: Config) -> list[Mount]:
    """The read-only mounts listed in sandbox.readonly_mounts. For work steps."""
    return [
        Mount(host=m.host, container=m.container, read_only=True)
        for m in config.sandbox.readonly_mounts
    ]


def _is_same_or_parent(path: Path, other: Path) -> bool:
    return path == other or path in other.parents


def _socket_paths() -> list[Path]:
    found: list[Path] = []
    for socket in DOCKER_SOCKETS:
        found.append(socket)
        found.append(socket.resolve())
    return found


def check_mounts(step: Step, mounts: Sequence[Mount], config: Config) -> list[Mount]:
    """Validate extra mounts for a step and return them. Raises SandboxError.

    Refused: any mount in a review step; the Docker socket or a folder that holds
    it; the state root, anything inside it or above it; the Codex login file or a
    folder above it; container paths that are relative, contain "..", or touch the
    paths the sandbox manages; paths with characters that break `--mount`.
    """
    mounts = list(mounts)
    if Step(step) is Step.REVIEW and mounts:
        raise SandboxError("review steps run without extra mounts")
    state_root = config.state_root.expanduser().resolve()
    auth_file = config.codex.auth_file.expanduser().resolve()
    seen: set[str] = set()
    for mount in mounts:
        raw_host = str(mount.host)
        if any(ch in raw_host or ch in mount.container for ch in _UNSAFE_MOUNT_CHARS):
            raise SandboxError(f"mount paths may not contain commas or quotes: {raw_host}")
        host = Path(mount.host).expanduser()
        if not host.is_absolute():
            raise SandboxError(f"mount host path must be absolute: {raw_host}")
        host_resolved = host.resolve()
        for candidate in {host, host_resolved}:
            for socket in _socket_paths():
                if _is_same_or_parent(candidate, socket):
                    raise SandboxError(f"the Docker socket may never be mounted ({raw_host})")
        if _is_same_or_parent(host_resolved, state_root) or state_root in host_resolved.parents:
            raise SandboxError(f"the state root may not be mounted into a step ({raw_host})")
        if _is_same_or_parent(host_resolved, auth_file):
            raise SandboxError(f"the Codex login file may not be mounted ({raw_host})")
        if not host_resolved.exists():
            raise SandboxError(f"mount host path does not exist: {raw_host}")
        container = PurePosixPath(mount.container)
        if not container.is_absolute() or ".." in container.parts or str(container) == "/":
            raise SandboxError(f"mount container path must be absolute and plain: {container}")
        for reserved in RESERVED_CONTAINER_PATHS:
            reserved_path = PurePosixPath(reserved)
            if (
                container == reserved_path
                or reserved_path in container.parents
                or container in reserved_path.parents
            ):
                raise SandboxError(f"{container} overlaps {reserved}, which the sandbox manages")
        if str(container) in seen:
            raise SandboxError(f"two mounts use the container path {container}")
        seen.add(str(container))
    return mounts


_HOST_MOUNT_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _host_mount_rule(kind: HostMountKind, config: Config) -> tuple[Path, str, bool, bool]:
    """(host base folder, container path or prefix, container path takes a /<name>, writable ok)"""
    if kind is HostMountKind.WORKTREE:
        return config.worktrees_dir, CONTAINER_REPOS, True, True
    if kind is HostMountKind.ARTIFACTS:
        return config.runs_dir, CONTAINER_ARTIFACTS, False, True
    if kind is HostMountKind.MCP:
        return config.runs_dir, CONTAINER_MCP, False, False
    if kind is HostMountKind.INSTRUCTIONS:
        return config.connectors_dir, CONTAINER_INSTRUCTIONS, True, False
    raise SandboxError(f"unknown host mount kind {kind!r}")


def check_host_mounts(step: Step, mounts: Sequence[HostMount], config: Config) -> list[HostMount]:
    """Validate host mounts for a step and return them. Raises SandboxError.

    Only work steps get host mounts. Each kind has one host folder it must come
    from and one container place it must land on:
      WORKTREE      worktrees/...   -> /opendot/repos/<name>          writable allowed
      ARTIFACTS     runs/...        -> /opendot/run/artifacts         writable allowed
      MCP           runs/...        -> /opendot/mcp                   read-only
      INSTRUCTIONS  connectors/...  -> /opendot/instructions/<name>   read-only
    The host folder must be a real folder strictly inside its base (no symbolic
    link on the way), which keeps out the database, worker.lock, sessions/,
    logs/ and the control clones in repos/. A WORKTREE folder must hold a .git
    folder, which docker_run_args mounts read-only on top.
    """
    mounts = list(mounts)
    if mounts and Step(step) is not Step.WORK:
        raise SandboxError("only work steps get host mounts")
    seen: set[str] = set()
    for mount in mounts:
        kind = HostMountKind(mount.kind)
        base, place, named, writable_ok = _host_mount_rule(kind, config)
        raw_host = str(mount.host)
        if any(ch in raw_host or ch in mount.container for ch in _UNSAFE_MOUNT_CHARS):
            raise SandboxError(f"mount paths may not contain commas or quotes: {raw_host}")
        host = Path(mount.host).expanduser()
        if not host.is_absolute():
            raise SandboxError(f"host mount path must be absolute: {raw_host}")
        host_resolved = _inside_without_links(host, base.expanduser())
        if host_resolved is None:
            raise SandboxError(
                f"a {kind.value} mount must be a folder inside {base}, "
                f"reached without symbolic links: {raw_host}"
            )
        container = PurePosixPath(mount.container)
        if named:
            if container.parent != PurePosixPath(place) or not _HOST_MOUNT_NAME.match(
                container.name
            ):
                raise SandboxError(f"a {kind.value} mount must land on {place}/<name>")
        elif container != PurePosixPath(place):
            raise SandboxError(f"a {kind.value} mount must land on {place}")
        if mount.writable and not writable_ok:
            raise SandboxError(f"a {kind.value} mount is always read-only")
        if kind is HostMountKind.WORKTREE:
            git_dir = host_resolved / ".git"
            if git_dir.is_symlink() or not git_dir.is_dir():
                raise SandboxError(f"a repository copy needs a .git folder: {raw_host}")
        if str(container) in seen:
            raise SandboxError(f"two mounts use the container path {container}")
        seen.add(str(container))
    return mounts


def _inside_without_links(host: Path, base: Path) -> Path | None:
    """The resolved folder when host is strictly inside base and no part below base
    is a symbolic link; otherwise None. base itself may be reached through links."""
    host_abs = Path(os.path.abspath(host))
    base_abs = Path(os.path.abspath(base))
    base_resolved = base.resolve()
    for root in (base_abs, base_resolved):
        if root in host_abs.parents:
            relative = host_abs.relative_to(root)
            break
    else:
        return None
    current = base_resolved
    for part in relative.parts:
        if part in ("", ".", ".."):
            return None
        current = current / part
        if current.is_symlink():
            return None
    if not current.is_dir():
        return None
    return current


def _host_mount_args(mount: HostMount) -> list[str]:
    host = Path(mount.host).expanduser().resolve()
    args = _mount_arg(host, mount.container, read_only=not mount.writable)
    if HostMountKind(mount.kind) is HostMountKind.WORKTREE:
        args += _mount_arg(host / ".git", f"{mount.container}/.git", read_only=True)
    return args


def _mount_arg(host: Path, container: str, read_only: bool) -> list[str]:
    spec = f"type=bind,src={host},dst={container}"
    if read_only:
        spec += ",readonly"
    return ["--mount", spec]


# ---------------------------------------------------------------------------
# Session folders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionDirs:
    """Host folders for one backend session: cli/ (the CLI's own files) and work/."""

    root: Path

    @property
    def cli(self) -> Path:
        return self.root / "cli"

    @property
    def work(self) -> Path:
        return self.root / "work"


def container_user() -> tuple[int, int]:
    """The uid and gid a step container runs as.

    Session folders are made by the host user with mode 0700, so the container
    runs with the same ids to be able to write them. When the host user is root,
    the container runs as CONTAINER_UID instead and the folders are handed to it.
    """
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        return CONTAINER_UID, CONTAINER_GID
    return uid, gid


def hand_to_container(path: Path) -> None:
    """Give a file or folder to the container user when the host user is root."""
    if os.getuid() == 0:
        os.chown(path, CONTAINER_UID, CONTAINER_GID, follow_symlinks=False)


def _sessions_root(config: Config, kind: str) -> Path:
    if not _SESSION_ID.match(kind):
        raise SandboxError(f"bad backend kind {kind!r}")
    return config.state_root / "sessions" / kind


def _make_session(root: Path) -> SessionDirs:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = SessionDirs(root)
    for path in (session.cli, session.work):
        path.mkdir(mode=0o700, exist_ok=True)
    for path in (session.root, session.cli, session.work):
        hand_to_container(path)
    return session


def new_session(config: Config, kind: str, session_id: str | None = None) -> SessionDirs:
    """Create folders for a new session. Without an id, a temporary name is used and
    adopt_session() renames the folder once the CLI reports its id."""
    name = session_id or f"new-{secrets.token_hex(8)}"
    if not _SESSION_ID.match(name):
        raise SandboxError(f"bad session id {name!r}")
    root = _sessions_root(config, kind)
    target = root / name
    if target.exists():
        raise SandboxError(f"session folder already exists for {name}")
    return _make_session(target)


def open_session(config: Config, kind: str, session_id: str) -> SessionDirs:
    """The folders of an earlier session, for a resumed step. Raises SandboxError if absent."""
    if not _SESSION_ID.match(session_id):
        raise SandboxError(f"bad session id {session_id!r}")
    target = _sessions_root(config, kind) / session_id
    if not target.is_dir() or target.is_symlink():
        raise SandboxError(f"no saved session {session_id} for the {kind} backend")
    return _make_session(target)


def adopt_session(session: SessionDirs, config: Config, kind: str, session_id: str) -> SessionDirs:
    """Rename a temporary session folder to the id the CLI reported."""
    if not _SESSION_ID.match(session_id):
        raise SandboxError(f"bad session id {session_id!r}")
    target = _sessions_root(config, kind) / session_id
    if target == session.root:
        return session
    if target.exists():
        raise SandboxError(f"session folder already exists for {session_id}")
    session.root.rename(target)
    return SessionDirs(target)


def remove_session(session: SessionDirs) -> None:
    shutil.rmtree(session.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# docker run
# ---------------------------------------------------------------------------


def container_name(kind: str, step: Step) -> str:
    return f"opendot-{kind.replace('_', '-')}-{Step(step).value}-{secrets.token_hex(6)}"


def docker_run_args(
    sandbox: SandboxConfig,
    *,
    name: str,
    command: Sequence[str],
    session: SessionDirs | None,
    work_writable: bool,
    mounts: Sequence[Mount] = (),
    env_names: Sequence[str] = (),
    fixed_env: Mapping[str, str] | None = None,
    user: tuple[int, int] | None = None,
    host_mounts: Sequence[HostMount] = (),
    shm_size: str = "",
    workdir: str = CONTAINER_WORKDIR,
) -> list[str]:
    """Build the `docker run` argument list for one step container.

    mounts must already have passed check_mounts(), and host_mounts
    check_host_mounts(). env_names are passed as bare `--env NAME`; their values
    must be in the environment of the docker process. fixed_env holds non-secret
    values (paths) written into the arguments. user is (uid, gid) and defaults to
    container_user(); root is refused. session=None starts a container without
    /work and /opendot/cli, for host jobs such as repository checks; pass a
    workdir that one of the mounts provides. shm_size sets --shm-size.
    """
    uid, gid = user or container_user()
    if uid == 0:
        raise SandboxError("step containers never run as root")
    if sandbox.network in ("", "host") or sandbox.network.startswith("container:"):
        raise SandboxError("step containers never share the host's or another container's network")
    args = [
        sandbox.docker,
        "run",
        "--rm",
        "--interactive",
        "--init",
        "--name",
        name,
        "--network",
        sandbox.network,
        "--cpus",
        f"{sandbox.cpus:g}",
        "--memory",
        sandbox.memory,
        "--memory-swap",
        sandbox.memory,
        "--pids-limit",
        str(sandbox.pids_limit),
        "--cap-drop",
        "ALL",
    ]
    if sandbox.no_new_privileges:
        args += ["--security-opt", "no-new-privileges"]
    if shm_size:
        args += ["--shm-size", shm_size]
    args += [
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={sandbox.tmp_size}",
        "--tmpfs",
        f"{CONTAINER_HOME}:rw,nosuid,nodev,size={sandbox.home_size},uid={uid},gid={gid},mode=0700",
        "--workdir",
        workdir,
    ]
    if session is not None:
        args += _mount_arg(session.work, CONTAINER_WORKDIR, read_only=not work_writable)
        args += _mount_arg(session.cli, CONTAINER_CLI_HOME, read_only=False)
    for mount in mounts:
        args += _mount_arg(Path(mount.host).expanduser().resolve(), mount.container, True)
    for host_mount in host_mounts:
        args += _host_mount_args(host_mount)
    env = {"HOME": CONTAINER_HOME, **(fixed_env or {})}
    for key, value in env.items():
        args += ["--env", f"{key}={value}"]
    for key in env_names:
        if not _ENV_NAME.match(key):
            raise SandboxError(f"{key!r} is not a valid variable name")
        args += ["--env", key]
    args.append(sandbox.image)
    args.extend(command)
    return args


def docker_kill_args(docker: str, name: str) -> list[str]:
    return [docker, "kill", name]


def stop_container(runner: CommandRunner, docker: str, name: str) -> None:
    """Kill the container by name. Killing the `docker run` client alone leaves it running."""
    try:
        runner.run(docker_kill_args(docker, name), timeout=30)
    except Exception:  # noqa: BLE001 - the container may already be gone
        pass


# ---------------------------------------------------------------------------
# MCP servers inside the step container (v0.2)
# ---------------------------------------------------------------------------


def _check_spec(spec: McpServerSpec) -> None:
    if not _HOST_MOUNT_NAME.match(spec.name) or "__" in spec.name:
        raise SandboxError(f"bad MCP server name {spec.name!r}")
    if not spec.command:
        raise SandboxError(f"MCP server {spec.name} has no command")
    for key in spec.env:
        if not _ENV_NAME.match(key) or key in RESERVED_ENV_NAMES:
            raise SandboxError(f"MCP server {spec.name} may not set {key}")
    for tool in spec.tools:
        if not _HOST_MOUNT_NAME.match(tool):
            raise SandboxError(f"bad MCP tool name {tool!r}")


def _toml_value(value: object) -> str:
    """A TOML value for a Codex -c override. JSON strings and arrays of strings
    are valid TOML; inline tables are written by hand."""
    if isinstance(value, dict):
        inner = ", ".join(f"{key} = {json.dumps(str(val))}" for key, val in value.items())
        return "{" + inner + "}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def codex_mcp_overrides(specs: Sequence[McpServerSpec]) -> list[str]:
    """`-c key=value` arguments that give the Codex CLI these stdio MCP servers.

    Tool calls are approved in advance (default_tools_approval_mode = "approve"),
    because the backend declines every question the CLI asks during a turn. Only
    the listed tools are enabled when spec.tools is not empty.
    """
    args: list[str] = []
    for spec in specs:
        _check_spec(spec)
        prefix = f"mcp_servers.{spec.name}"
        values: dict[str, object] = {
            "command": spec.command[0],
            "args": list(spec.command[1:]),
            "startup_timeout_sec": spec.startup_timeout_seconds,
            "tool_timeout_sec": spec.tool_timeout_seconds,
            "default_tools_approval_mode": "approve",
        }
        if spec.env:
            values["env"] = dict(spec.env)
        if spec.tools:
            values["enabled_tools"] = list(spec.tools)
        for key, value in values.items():
            args += ["-c", f"{prefix}.{key}={_toml_value(value)}"]
    return args


def claude_mcp_config(specs: Sequence[McpServerSpec]) -> dict[str, object]:
    """The JSON object for Claude Code's --mcp-config (used with --strict-mcp-config)."""
    servers: dict[str, object] = {}
    for spec in specs:
        _check_spec(spec)
        entry: dict[str, object] = {
            "type": "stdio",
            "command": spec.command[0],
            "args": list(spec.command[1:]),
        }
        if spec.env:
            entry["env"] = dict(spec.env)
        servers[spec.name] = entry
    return {"mcpServers": servers}


def claude_mcp_allowed_tools(specs: Sequence[McpServerSpec]) -> list[str]:
    """Names for --allowedTools: mcp__<server>__<tool>, or mcp__<server> for every tool."""
    names: list[str] = []
    for spec in specs:
        _check_spec(spec)
        if spec.tools:
            names.extend(f"mcp__{spec.name}__{tool}" for tool in spec.tools)
        else:
            names.append(f"mcp__{spec.name}")
    return names


# ---------------------------------------------------------------------------
# Docker installed as a snap
# ---------------------------------------------------------------------------

SNAP_DOCKER_ADVICE = """\
Docker on this machine is the snap package. Its security profile refuses to start
containers that use --security-opt no-new-privileges ("operation not permitted").
Set this in opendot.toml:

    [sandbox]
    no_new_privileges = false

Trade-off: every capability is still dropped (--cap-drop ALL), the root file
system is still read-only and the container still runs as a non-root user. What
you lose is the kernel flag that stops a process from gaining rights through a
setuid or setgid program inside the image. Docker from docker.com does not have
this limit.

Snap Docker also cannot see folders under /tmp. OpenDot mounts only folders
under your home folder (the state root and your sandbox.readonly_mounts), so
keep state_root and every readonly_mounts entry out of /tmp."""


def detect_snap_docker(
    docker: str,
    docker_root_dir: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    """True when the docker command is the snap package.

    Two signs, either is enough: the command resolves to a path under /snap, or
    the daemon's root folder (`docker info --format {{.DockerRootDir}}`, passed in
    as docker_root_dir) is under /var/snap.
    """
    found = which(docker)
    if found:
        resolved = Path(str(found)).resolve()
        for candidate in (Path(str(found)), resolved):
            if candidate.parts[:2] == ("/", "snap"):
                return True
    if docker_root_dir and PurePosixPath(docker_root_dir).parts[:3] == ("/", "var", "snap"):
        return True
    return False


def snap_docker_problems(config: Config, is_snap: bool) -> list[str]:
    """Config settings that do not work with snap Docker. Empty when all is well."""
    if not is_snap:
        return []
    problems = []
    if config.sandbox.no_new_privileges:
        problems.append("sandbox.no_new_privileges is true, which snap Docker refuses")
    for path in (config.state_root, *(m.host for m in config.sandbox.readonly_mounts)):
        resolved = Path(path).expanduser().resolve()
        if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
            problems.append(f"{path} is under /tmp, which snap Docker cannot mount")
    return problems
