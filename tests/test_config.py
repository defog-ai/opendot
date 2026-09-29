import os
import stat
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from opendot.actions import ActionRegistry, InvalidProposal, PreparedAction, UnknownAction
from opendot.backends import BackendError, StepInterrupted, StepTimedOut, create_backend
from opendot.backends.fake import FakeBackend
from opendot.config import DEFAULTS, ENV_OVERRIDES, Config, ConfigError
from opendot.models import Level, Step

EXAMPLE = Path(__file__).resolve().parent.parent / "opendot.example.toml"


def test_defaults():
    cfg = Config.from_dict({}, env={})
    assert cfg.core.timezone == "UTC"
    assert cfg.worker_backend.kind == "codex"
    assert cfg.reviewer_backend.kind == "claude_code"
    assert cfg.sandbox.network == "bridge"
    assert cfg.slack.enabled is False
    assert cfg.slack.allowed_users == []
    assert cfg.fake.script is None
    assert cfg.rules == []
    assert cfg.db_path == cfg.state_root / "opendot.db"


def test_load_reads_file_and_env_wins(tmp_path):
    path = tmp_path / "opendot.toml"
    path.write_text(
        '[core]\ntimezone = "Asia/Tokyo"\nlease_minutes = 5\n'
        '[channels.slack]\nallowed_users = ["U_ALICE"]\n'
    )
    env = {
        "OPENDOT_CORE_LEASE_MINUTES": "9",
        "OPENDOT_CHANNELS_SLACK_ENABLED": "yes",
        "OPENDOT_CHANNELS_SLACK_ALLOWED_USERS": "U_ALICE, U_BOB",
        "OPENDOT_SANDBOX_CPUS": "1.5",
        "OPENDOT_BACKEND_CLAUDE_CODE_TOKEN_ENV": "MY_TOKEN",
        "HOME": "/tmp/elsewhere",
    }
    cfg = Config.load(path, env=env)
    assert cfg.source_path == path
    assert cfg.core.timezone == "Asia/Tokyo"
    assert cfg.core.lease_minutes == 9
    assert cfg.slack.enabled is True
    assert cfg.slack.allowed_users == ["U_ALICE", "U_BOB"]
    assert cfg.sandbox.cpus == 1.5
    assert cfg.claude_code.token_env == "MY_TOKEN"


def test_load_uses_config_env_var(tmp_path):
    path = tmp_path / "other.toml"
    path.write_text('[core]\ntimezone = "Europe/Paris"\n')
    assert Config.load(env={"OPENDOT_CONFIG": str(path)}).core.timezone == "Europe/Paris"
    with pytest.raises(ConfigError):
        Config.load(env={"OPENDOT_CONFIG": str(tmp_path / "missing.toml")})
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "missing.toml", env={})


def test_bad_env_value():
    with pytest.raises(ConfigError):
        Config.from_dict({}, env={"OPENDOT_CORE_LEASE_MINUTES": "soon"})
    with pytest.raises(ConfigError):
        Config.from_dict({}, env={"OPENDOT_CHANNELS_CLI_ENABLED": "maybe"})


@pytest.mark.parametrize(
    "data",
    [
        {"core": {"statroot": "/x"}},
        {"nonsense": {}},
        {"core": {"lease_minutes": "60"}},
        {"channels": {"slack": {"enabled": 1}}},
        {"core": {"timezone": "Mars/Olympus"}},
        {"sandbox": {"network": "host"}},
        {"sandbox": {"network": ""}},
        {"sandbox": {"network": "container:some-other"}},
        {"sandbox": {"env_allowlist": ["OPENDOT_SLACK_BOT_TOKEN"]}},
        {"sandbox": {"env_allowlist": ["CLAUDE_CODE_OAUTH_TOKEN"]}},
        {
            "channels": {"slack": {"bot_token_env": "MY_BOT"}},
            "sandbox": {"env_allowlist": ["MY_BOT"]},
        },
        {"backend": {"worker": {"kind": "nope"}}},
        {"sandbox": {"readonly_mounts": [{"host": "/a", "container": "relative"}]}},
        {"sandbox": {"readonly_mounts": [{"host": "/a"}]}},
        {"rules": [{"kind": "note.write", "level": "refuse"}]},
        {"rules": [{"kind": "note.write"}]},
        {"limits": {"task_minutes": -1}},
        {"reviewer": {"denials_in_a_row": 0}},
    ],
)
def test_invalid_config_is_refused(data):
    with pytest.raises(ConfigError):
        Config.from_dict(data, env={})


def test_host_network_refused_from_env_too():
    with pytest.raises(ConfigError):
        Config.from_dict({}, env={"OPENDOT_SANDBOX_NETWORK": "host"})


def test_rules_and_mounts_parse():
    cfg = Config.from_dict(
        {
            "rules": [{"kind": "note.write", "level": "ask"}],
            "sandbox": {"readonly_mounts": [{"host": "/srv/ref", "container": "/evidence/ref"}]},
        },
        env={},
    )
    assert cfg.rules[0].target == "*"
    assert cfg.rules[0].level is Level.ASK
    assert cfg.sandbox.readonly_mounts[0].host == Path("/srv/ref")


def test_empty_allowed_users_allows_nobody():
    cfg = Config.from_dict({"channels": {"slack": {"enabled": True}}}, env={})
    assert not cfg.slack.is_allowed("U_ALICE")
    assert not cfg.slack.is_allowed("")
    cfg = Config.from_dict({"channels": {"slack": {"allowed_users": ["U_ALICE"]}}}, env={})
    assert cfg.slack.is_allowed("U_ALICE")
    assert not cfg.slack.is_allowed("U_BOB")


def test_bot_token_reads_named_variable():
    cfg = Config.from_dict({}, env={})
    assert cfg.slack.bot_token({}) is None
    assert cfg.slack.bot_token({"OPENDOT_SLACK_BOT_TOKEN": "xoxb-test"}) == "xoxb-test"


def test_state_root_is_private(tmp_path):
    root = tmp_path / "a" / "state"
    cfg = Config.from_dict({"core": {"state_root": str(root)}}, env={})
    cfg.ensure_directories()
    for path in (cfg.state_root, cfg.runs_dir, cfg.logs_dir):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    os.chmod(root, 0o755)
    cfg.ensure_directories()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


def test_config_fixture(config, state_root):
    assert config.state_root == state_root
    assert state_root.is_dir()
    assert config.backend_choice("worker").kind == "fake"
    with pytest.raises(ValueError):
        config.backend_choice("judge")


def test_example_file_matches_defaults():
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    table_lists = {"rules", "repositories", "mcp_servers"}
    assert data == {k: v for k, v in DEFAULTS.items() if k not in table_lists}
    loaded = Config.load(EXAMPLE, env={})
    assert loaded == replace(Config.from_dict({}, env={}), source_path=EXAMPLE)


def test_env_names_are_unique_and_cover_scalars():
    assert len(set(ENV_OVERRIDES.values())) == len(ENV_OVERRIDES)
    assert ENV_OVERRIDES["OPENDOT_BACKEND_CLAUDE_CODE_TOKEN_ENV"] == (
        "backend",
        "claude_code",
        "token_env",
    )
    assert "OPENDOT_RULES" not in ENV_OVERRIDES
    assert "OPENDOT_SANDBOX_READONLY_MOUNTS" not in ENV_OVERRIDES


# -- fake backend and action registry --------------------------------------


def test_fake_backend_from_config(tmp_path, config):
    script = tmp_path / "script.json"
    script.write_text('{"work": [{"output": {"summary": "hi"}, "usage": {"turns": 1}}]}')
    cfg = Config.from_dict(
        {
            "core": {"state_root": str(config.state_root)},
            "backend": {"worker": {"kind": "fake"}, "fake": {"script": str(script)}},
        },
        env={},
    )
    backend = create_backend(cfg, "worker")
    assert isinstance(backend, FakeBackend)
    result = backend.run_step(Step.WORK, "p", {}, {}, [])
    assert result.output == {"summary": "hi"}
    assert result.usage == {"turns": 1}
    assert result.thread_id == "fake-thread-1"


def test_fake_backend_scripted_outcomes(fake_backend):
    fake_backend.push(Step.WORK, {"a": 1}, thread_id="t-9")
    fake_backend.push_error(Step.WORK, "crashed")
    fake_backend.push_interrupt(Step.WORK)
    fake_backend.push_timeout(Step.REVIEW)
    fake_backend.push(Step.REVIEW, {"verdict": "approve"})
    assert fake_backend.run_step(Step.WORK, "p", {}, {}, []).thread_id == "t-9"
    with pytest.raises(BackendError, match="crashed"):
        fake_backend.run_step(Step.WORK, "p", {}, {}, [])
    with pytest.raises(StepInterrupted):
        fake_backend.run_step(Step.WORK, "p", {}, {}, [])
    with pytest.raises(BackendError):
        fake_backend.run_step(Step.WORK, "p", {}, {}, [])
    with pytest.raises(StepTimedOut):
        fake_backend.run_step(Step.REVIEW, "p", {}, {}, [])
    with pytest.raises(StepInterrupted):
        fake_backend.run_step(Step.REVIEW, "p", {}, {}, [], should_stop=lambda: True)
    assert fake_backend.remaining(Step.REVIEW) == 1
    assert fake_backend.run_step(Step.REVIEW, "p", {}, {}, [], "resume-1").thread_id == "resume-1"
    assert len(fake_backend.calls_for(Step.WORK)) == 4


class _EchoHandler:
    kind = "reply.post"
    outward = True
    default_level = Level.ALLOW
    floor = Level.ALLOW

    def __init__(self, returned_kind="reply.post"):
        self.returned_kind = returned_kind

    def prepare(self, proposal, ctx):
        return PreparedAction(self.returned_kind, "fake:local:1", {"text": proposal["text"]}, True)

    def execute(self, action, ctx):
        raise AssertionError("not used")


def test_registry_refuses_unknown_kinds():
    registry = ActionRegistry([_EchoHandler()])
    prepared = registry.prepare({"kind": "reply.post", "text": "hi"}, ctx=None)
    assert prepared.target == "fake:local:1"
    assert len(prepared.digest) == 64
    with pytest.raises(UnknownAction):
        registry.prepare({"kind": "shell.run", "cmd": "ls"}, ctx=None)
    with pytest.raises(UnknownAction):
        registry.prepare({"text": "no kind"}, ctx=None)
    with pytest.raises(InvalidProposal):
        registry.prepare(["reply.post"], ctx=None)
    with pytest.raises(ValueError):
        registry.register(_EchoHandler())


def test_registry_rejects_kind_swap_and_loose_default():
    registry = ActionRegistry([_EchoHandler(returned_kind="notify.post")])
    with pytest.raises(InvalidProposal):
        registry.prepare({"kind": "reply.post", "text": "hi"}, ctx=None)

    loose = _EchoHandler()
    loose.kind = "note.write"
    loose.floor = Level.ASK
    with pytest.raises(ValueError):
        ActionRegistry([loose])
