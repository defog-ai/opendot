"""Configuration: one TOML file, then OPENDOT_* environment variables on top.

Every scalar or string-list key has an override named OPENDOT_<SECTION>_<KEY>, with
dots replaced by underscores and the whole name upper-cased, for example
core.state_root -> OPENDOT_CORE_STATE_ROOT and
backend.claude_code.token_env -> OPENDOT_BACKEND_CLAUDE_CODE_TOKEN_ENV.
List values in the environment are comma-separated. Tables of tables
([[rules]], sandbox.readonly_mounts) have no environment override.
"""

from __future__ import annotations

import copy
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opendot.models import Level

ENV_PREFIX = "OPENDOT_"
CONFIG_ENV = "OPENDOT_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/opendot/opendot.toml")
BACKEND_ROLES = ("worker", "reviewer")

DEFAULTS: dict[str, Any] = {
    "core": {
        "state_root": "~/.local/state/opendot",
        "timezone": "UTC",
        "lease_minutes": 60,
    },
    "limits": {
        "task_minutes": 120,
        "step_minutes": 30,
        "max_steps_per_task": 20,
        "max_turns_per_task": 200,
        "max_tokens_per_task": 0,
    },
    "reviewer": {
        "denials_in_a_row": 3,
        "denial_window": 50,
        "denials_in_window": 10,
    },
    "backend": {
        "worker": {"kind": "codex", "model": ""},
        "reviewer": {"kind": "claude_code", "model": ""},
        "codex": {"auth_file": "~/.codex/auth.json"},
        "claude_code": {"token_env": "CLAUDE_CODE_OAUTH_TOKEN"},
        "fake": {"script": ""},
    },
    "sandbox": {
        "image": "opendot-worker:latest",
        "docker": "docker",
        "network": "bridge",
        "cpus": 2.0,
        "memory": "4g",
        "pids_limit": 512,
        "tmp_size": "1g",
        "home_size": "512m",
        "env_allowlist": [],
        "readonly_mounts": [],
    },
    "channels": {
        "cli": {"enabled": True, "user": "operator"},
        "slack": {
            "enabled": False,
            "bot_token_env": "OPENDOT_SLACK_BOT_TOKEN",
            "channels": [],
            "allowed_users": [],
        },
    },
    "rules": [],
}

# Keys whose values are lists of tables; everything else in DEFAULTS is a table or a scalar.
_TABLE_LISTS = {("sandbox", "readonly_mounts"), ("rules",)}


class ConfigError(ValueError):
    pass


def _env_paths(tree: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    """Map every overridable key path to its environment variable name."""
    names: dict[str, tuple[str, ...]] = {}
    for key, value in tree.items():
        path = (*prefix, key)
        if path in _TABLE_LISTS:
            continue
        if isinstance(value, dict):
            names.update(_env_paths(value, path))
        else:
            name = ENV_PREFIX + "_".join(path).upper()
            if name in names:
                raise RuntimeError(f"two config keys map to {name}")
            names[name] = path
    return names


ENV_OVERRIDES: dict[str, tuple[str, ...]] = _env_paths(DEFAULTS)


def _parse_env_value(name: str, raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{name} must be true or false, got {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if isinstance(default, list):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def _merge(base: dict[str, Any], override: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> None:
    for key, value in override.items():
        path = (*prefix, key)
        dotted = ".".join(path)
        if key not in base:
            raise ConfigError(f"unknown config key {dotted}")
        default = base[key]
        if isinstance(default, dict) and path not in _TABLE_LISTS:
            if not isinstance(value, Mapping):
                raise ConfigError(f"{dotted} must be a table")
            _merge(default, value, path)
            continue
        if isinstance(default, bool) != isinstance(value, bool):
            raise ConfigError(f"{dotted} has the wrong type")
        if isinstance(default, float) and isinstance(value, int):
            value = float(value)
        if not isinstance(value, type(default)):
            raise ConfigError(f"{dotted} must be of type {type(default).__name__}")
        base[key] = value


def _get(tree: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = tree
    for key in path:
        node = node[key]
    return node


def _set(tree: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = tree
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


# ---------------------------------------------------------------------------
# Typed configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreConfig:
    state_root: Path
    timezone: str
    lease_minutes: int


@dataclass(frozen=True)
class LimitsConfig:
    """Per-task budgets. 0 means no limit. There is no money limit on purpose."""

    task_minutes: int  # active (step) time, summed over the task
    step_minutes: int  # wall-clock limit for one backend step
    max_steps_per_task: int
    max_turns_per_task: int
    max_tokens_per_task: int


@dataclass(frozen=True)
class ReviewerConfig:
    denials_in_a_row: int
    denial_window: int
    denials_in_window: int


@dataclass(frozen=True)
class BackendChoice:
    kind: str  # "codex" | "claude_code" | "fake" | "anthropic_api"
    model: str  # "" = the CLI's own default model


@dataclass(frozen=True)
class CodexConfig:
    auth_file: Path  # only this file is copied into the container


@dataclass(frozen=True)
class ClaudeCodeConfig:
    token_env: str  # host variable holding the token; passed only to the backend process


@dataclass(frozen=True)
class FakeBackendConfig:
    script: Path | None  # JSON file of scripted outputs


@dataclass(frozen=True)
class ReadonlyMount:
    host: Path
    container: str


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    docker: str
    network: str
    cpus: float
    memory: str
    pids_limit: int
    tmp_size: str
    home_size: str
    env_allowlist: list[str]
    readonly_mounts: list[ReadonlyMount]


@dataclass(frozen=True)
class CliChannelConfig:
    enabled: bool
    user: str  # requester name for tasks created on the command line


@dataclass(frozen=True)
class SlackChannelConfig:
    enabled: bool
    bot_token_env: str
    channels: list[str]
    allowed_users: list[str]

    def is_allowed(self, user_id: str) -> bool:
        """An empty allowed_users list allows nobody."""
        return user_id in self.allowed_users

    def bot_token(self, env: Mapping[str, str] | None = None) -> str | None:
        source = os.environ if env is None else env
        return source.get(self.bot_token_env) or None


@dataclass(frozen=True)
class RuleConfig:
    kind: str
    target: str
    level: Level


@dataclass(frozen=True)
class Config:
    core: CoreConfig
    limits: LimitsConfig
    reviewer: ReviewerConfig
    worker_backend: BackendChoice
    reviewer_backend: BackendChoice
    codex: CodexConfig
    claude_code: ClaudeCodeConfig
    fake: FakeBackendConfig
    sandbox: SandboxConfig
    cli: CliChannelConfig
    slack: SlackChannelConfig
    rules: list[RuleConfig] = field(default_factory=list)
    source_path: Path | None = None

    # -- derived paths -------------------------------------------------------

    @property
    def state_root(self) -> Path:
        return self.core.state_root

    @property
    def db_path(self) -> Path:
        return self.core.state_root / "opendot.db"

    @property
    def lock_path(self) -> Path:
        return self.core.state_root / "worker.lock"

    @property
    def runs_dir(self) -> Path:
        return self.core.state_root / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.core.state_root / "logs"

    def ensure_directories(self) -> None:
        """Create the state root, runs/ and logs/ with mode 0700; tighten them if they exist."""
        for path in (self.state_root, self.runs_dir, self.logs_dir):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def backend_choice(self, role: str) -> BackendChoice:
        if role == "worker":
            return self.worker_backend
        if role == "reviewer":
            return self.reviewer_backend
        raise ValueError(f"unknown backend role {role!r}; expected one of {BACKEND_ROLES}")

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
        """Read the TOML file, apply OPENDOT_* overrides from env, validate.

        path: explicit file (must exist). Otherwise OPENDOT_CONFIG (must exist), otherwise
        ~/.config/opendot/opendot.toml if it exists, otherwise the built-in defaults.
        env: defaults to os.environ.
        """
        env = os.environ if env is None else env
        source: Path | None = None
        if path is not None:
            source = Path(path).expanduser()
        elif env.get(CONFIG_ENV):
            source = Path(env[CONFIG_ENV]).expanduser()
        elif DEFAULT_CONFIG_PATH.expanduser().exists():
            source = DEFAULT_CONFIG_PATH.expanduser()
        data: dict[str, Any] = {}
        if source is not None:
            if not source.is_file():
                raise ConfigError(f"config file not found: {source}")
            try:
                data = tomllib.loads(source.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{source}: {exc}") from None
        return cls.from_dict(data, env=env, source_path=source)

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
        source_path: Path | None = None,
    ) -> Config:
        """Build a Config from parsed TOML data plus env overrides (env=None: no overrides)."""
        merged = copy.deepcopy(DEFAULTS)
        _merge(merged, data)
        for name, raw in (env or {}).items():
            if name in ENV_OVERRIDES:
                key_path = ENV_OVERRIDES[name]
                _set(merged, key_path, _parse_env_value(name, raw, _get(DEFAULTS, key_path)))
        return _build(merged, source_path)


def _positive(value: int | float, name: str, *, allow_zero: bool = False) -> None:
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{name} must be {'zero or more' if allow_zero else 'positive'}")


def _build(data: dict[str, Any], source_path: Path | None) -> Config:
    from opendot.backends import known_backend_kinds

    core = data["core"]
    try:
        ZoneInfo(core["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"core.timezone is not a known time zone: {core['timezone']!r}") from None
    _positive(core["lease_minutes"], "core.lease_minutes")

    limits = data["limits"]
    for key, value in limits.items():
        _positive(value, f"limits.{key}", allow_zero=True)

    reviewer = data["reviewer"]
    for key, value in reviewer.items():
        _positive(value, f"reviewer.{key}")

    backend = data["backend"]
    kinds = known_backend_kinds()
    choices = {}
    for role in BACKEND_ROLES:
        kind = backend[role]["kind"]
        if kind not in kinds:
            raise ConfigError(f"backend.{role}.kind must be one of {sorted(kinds)}, got {kind!r}")
        choices[role] = BackendChoice(kind=kind, model=backend[role]["model"])

    sandbox = data["sandbox"]
    if sandbox["network"] == "host" or str(sandbox["network"]).startswith("container:"):
        raise ConfigError(
            f"sandbox.network = {sandbox['network']!r} would share another network "
            "namespace instead of isolating the step container"
        )
    if not sandbox["network"]:
        raise ConfigError("sandbox.network must name a Docker network, or 'none'")
    _positive(sandbox["cpus"], "sandbox.cpus")
    _positive(sandbox["pids_limit"], "sandbox.pids_limit")
    mounts = []
    for index, item in enumerate(sandbox["readonly_mounts"]):
        where = f"sandbox.readonly_mounts[{index}]"
        if not isinstance(item, Mapping) or set(item) != {"host", "container"}:
            raise ConfigError(f"{where} must have exactly the keys host and container")
        if not str(item["container"]).startswith("/"):
            raise ConfigError(f"{where}.container must be an absolute path")
        mounts.append(
            ReadonlyMount(host=_expand(str(item["host"])), container=str(item["container"]))
        )
    secret_names = {
        data["channels"]["slack"]["bot_token_env"],
        backend["claude_code"]["token_env"],
    }
    for name in sandbox["env_allowlist"]:
        if not isinstance(name, str) or not name:
            raise ConfigError("sandbox.env_allowlist must hold variable names")
        if name in secret_names:
            raise ConfigError(
                f"sandbox.env_allowlist may not name {name}, which holds a login the host keeps"
            )

    rules = []
    for index, item in enumerate(data["rules"]):
        where = f"rules[{index}]"
        if not isinstance(item, Mapping) or not {"kind", "level"} <= set(item) <= {
            "kind",
            "target",
            "level",
        }:
            raise ConfigError(f"{where} needs kind and level, and may have target")
        try:
            level = Level(item["level"])
        except ValueError:
            raise ConfigError(
                f"{where}.level must be one of {[lvl.value for lvl in Level]}"
            ) from None
        rules.append(
            RuleConfig(kind=str(item["kind"]), target=str(item.get("target", "*")), level=level)
        )

    slack = data["channels"]["slack"]
    cli = data["channels"]["cli"]
    for key in ("channels", "allowed_users"):
        if not all(isinstance(v, str) for v in slack[key]):
            raise ConfigError(f"channels.slack.{key} must be a list of strings")

    fake_script = backend["fake"]["script"]
    return Config(
        core=CoreConfig(
            state_root=_expand(core["state_root"]),
            timezone=core["timezone"],
            lease_minutes=core["lease_minutes"],
        ),
        limits=LimitsConfig(**limits),
        reviewer=ReviewerConfig(**reviewer),
        worker_backend=choices["worker"],
        reviewer_backend=choices["reviewer"],
        codex=CodexConfig(auth_file=_expand(backend["codex"]["auth_file"])),
        claude_code=ClaudeCodeConfig(token_env=backend["claude_code"]["token_env"]),
        fake=FakeBackendConfig(script=_expand(fake_script) if fake_script else None),
        sandbox=SandboxConfig(
            image=sandbox["image"],
            docker=sandbox["docker"],
            network=sandbox["network"],
            cpus=sandbox["cpus"],
            memory=sandbox["memory"],
            pids_limit=sandbox["pids_limit"],
            tmp_size=sandbox["tmp_size"],
            home_size=sandbox["home_size"],
            env_allowlist=list(sandbox["env_allowlist"]),
            readonly_mounts=mounts,
        ),
        cli=CliChannelConfig(enabled=cli["enabled"], user=cli["user"]),
        slack=SlackChannelConfig(
            enabled=slack["enabled"],
            bot_token_env=slack["bot_token_env"],
            channels=list(slack["channels"]),
            allowed_users=list(slack["allowed_users"]),
        ),
        rules=rules,
        source_path=source_path,
    )
