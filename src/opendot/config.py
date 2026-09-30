"""Configuration: one TOML file, then OPENDOT_* environment variables on top.

Every scalar or string-list key has an override named OPENDOT_<SECTION>_<KEY>, with
dots replaced by underscores and the whole name upper-cased, for example
core.state_root -> OPENDOT_CORE_STATE_ROOT and
backend.claude_code.token_env -> OPENDOT_BACKEND_CLAUDE_CODE_TOKEN_ENV.
List values in the environment are comma-separated. Tables of tables
([[repositories]], [[mcp_servers]], sandbox.readonly_mounts) have no environment
override.

OpenDot 0.3 removed the reviewer, the approvals and the rules. A file that still
has [reviewer], [backend.reviewer] or [[rules]] loads: those keys are ignored and
listed in Config.ignored_keys, which `opendot doctor` reports.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENV_PREFIX = "OPENDOT_"
CONFIG_ENV = "OPENDOT_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/opendot/opendot.toml")
BACKEND_ROLES = ("worker",)

# Keys that OpenDot 0.3 removed. A config file that has them still loads.
RETIRED_KEYS = (("reviewer",), ("backend", "reviewer"), ("rules",))

# FactIQ preset. Only public facts: the connector address and the public plugin
# repository, whose skill files are shared with the model read-only (MIT licence).
FACTIQ_URL = "https://api.factiq.com/mcp"
FACTIQ_PLUGIN_REPO = "https://github.com/defog-ai/factiq-plugin"
FACTIQ_PLUGIN_COMMIT = "b427ec50acd6cd9258cbb7fd136f7ed2f0e02807"
FACTIQ_READ_TOOLS = (
    "get_data_catalog",
    "search_datasets",
    "describe_dataset",
    "search_series",
    "run_sql",
    "get_series",
    "get_market_data",
    "get_geo_data",
    "search_company_filings",
    "search_earnings_transcripts",
    "search_media_appearances",
    "search_news",
    "get_style_guides",
)
FACTIQ_WRITE_TOOLS = ("send_feedback",)

# Browser tools the model may call. Left out on purpose: page scripts
# (browser_evaluate, browser_run_code_unsafe), file upload, cookie and storage
# tools, and saved login state.
DEFAULT_BROWSER_TOOLS = [
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_click",
    "browser_hover",
    "browser_type",
    "browser_press_key",
    "browser_select_option",
    "browser_wait_for",
    "browser_tabs",
    "browser_resize",
    "browser_close",
    "browser_console_messages",
    "browser_network_requests",
]

DEFAULT_FORBIDDEN_FILES = [
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ecdsa",
    "id_ed25519",
]

# Names of connectors and repositories. No dots (they are part of action kinds
# such as mcp.<server>.<tool>) and no double underscore (Claude Code joins
# server and tool names with one).
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_SERVER_NAMES = frozenset({"browser", "opendot"})
MCP_AUTH_KINDS = ("none", "bearer_env", "oauth")
MCP_TOOL_MODES = ("read", "write")

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
    "backend": {
        "worker": {"kind": "codex", "model": ""},
        "codex": {"auth_file": "~/.codex/auth.json"},
        "claude_code": {
            "token_env": "CLAUDE_CODE_OAUTH_TOKEN",
            "token_file": "~/.config/opendot/claude-token",
        },
        "opencode": {"auth_file": "~/.local/share/opencode/auth.json"},
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
        "no_new_privileges": True,
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
    "github": {
        "token_env": "OPENDOT_GITHUB_TOKEN",
        "api_url": "https://api.github.com",
        "host": "github.com",
        "author_name": "OpenDot",
        "author_email": "",
        "committer_name": "",
        "committer_email": "",
        "signing_key": "",
        "branch_prefix": "opendot/",
        "private_markers": [],
        "forbidden_files": list(DEFAULT_FORBIDDEN_FILES),
        "max_file_kib": 512,
        "check_minutes": 15,
        "max_diff_chars": 20_000,
        "allow_binary_public": False,
    },
    "repositories": [],
    "browser": {
        "enabled": False,
        "viewport": "1280x800",
        "allowed_origins": [],
        "shm_size": "1g",
        "tools": list(DEFAULT_BROWSER_TOOLS),
    },
    "gateway": {
        "call_timeout_seconds": 120,
        "max_result_kib": 512,
    },
    "mcp_servers": [],
    "factiq": {
        "enabled": False,
        "url": FACTIQ_URL,
        "auth": "bearer_env",
        "api_key_env": "FACTIQ_API_KEY",
        "instructions": True,
        "feedback": False,
    },
}

# Keys whose values are lists of tables; everything else in DEFAULTS is a table or a scalar.
_TABLE_LISTS = {
    ("sandbox", "readonly_mounts"),
    ("repositories",),
    ("mcp_servers",),
}


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
class BackendChoice:
    kind: str  # "codex" | "claude_code" | "opencode" | "fake" | "anthropic_api"
    model: str  # "" = the CLI's own default model


@dataclass(frozen=True)
class CodexConfig:
    auth_file: Path  # only this file is copied into the container


@dataclass(frozen=True)
class ClaudeCodeConfig:
    token_env: str  # host variable holding the token; passed only to the backend process
    token_file: Path  # saved by `opendot login claude`; read when token_env is not set


@dataclass(frozen=True)
class OpencodeConfig:
    auth_file: Path  # only the entry for the model's provider is copied into the container


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
    # --security-opt no-new-privileges. Docker installed as a snap refuses to start
    # containers with it ("operation not permitted"); set false there. See doctor.
    no_new_privileges: bool = True


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
class GithubConfig:
    token_env: str  # host variable holding the GitHub token; never enters a container
    api_url: str
    host: str  # the git host remotes must name for GitHub actions
    author_name: str
    author_email: str  # "" = the token owner's GitHub no-reply address
    committer_name: str  # "" = author_name
    committer_email: str  # "" = author_email
    signing_key: Path | None  # SSH key file for signed commits; None = unsigned
    branch_prefix: str
    private_markers: list[str]  # extra literal strings refused in pushes to public repos
    forbidden_files: list[str]  # glob patterns matched against each changed path's name
    max_file_kib: int
    check_minutes: int  # wall-clock limit for one repository's checks
    # The longest diff kept with a push or pull request. A longer change is refused,
    # never cut.
    max_diff_chars: int = 20_000
    # Off by default: allow binary files in a push to a public repository. The diff
    # shows only their names and sizes.
    allow_binary_public: bool = False

    def token(self, env: Mapping[str, str] | None = None) -> str | None:
        source = os.environ if env is None else env
        return source.get(self.token_env) or None


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    remote: str
    default_branch: str
    prepare: list[str]  # shell commands run in a sandbox container before the work step
    checks: list[str]  # shell commands run in a separate sandbox container before a push
    public: bool  # the operator allows pushes to this repository while it is public
    check_network: str  # Docker network for the checks container; "none" by default
    # "github" (default): visibility, pull requests, issues and comments go through
    # the GitHub API. "none": a plain git remote (any address git can push to,
    # including a local folder). Only github.push_branch works for it, and the
    # operator states its visibility below instead of GitHub reporting it.
    forge: str = "github"
    visibility: str = ""  # "private" or "public"; required when forge = "none"

    @property
    def plain_git(self) -> bool:
        return self.forge == "none"

    def github_slug(self, host: str) -> tuple[str, str] | None:
        """(owner, repo) when the remote points at host, else None."""
        if self.plain_git:
            return None
        return parse_github_remote(self.remote, host)


@dataclass(frozen=True)
class BrowserConfig:
    enabled: bool
    viewport: str  # "WIDTHxHEIGHT"
    allowed_origins: list[str]  # passed to the browser; not a security boundary
    shm_size: str  # /dev/shm size for the step container when the browser is on
    tools: list[str]


@dataclass(frozen=True)
class GatewayConfig:
    call_timeout_seconds: int
    max_result_kib: int


@dataclass(frozen=True)
class McpToolConfig:
    name: str
    mode: str  # "read": the model may call it; "write": only as an action


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    url: str  # streamable HTTP address, or ""
    command: list[str]  # host command for a stdio server, or []
    auth: str  # "none" | "bearer_env" | "oauth"
    auth_env: str  # host variable holding the bearer token when auth = "bearer_env"
    tools: list[McpToolConfig]
    instructions: Path | None = None  # read-only folder shown to the model
    preset: str = ""  # "factiq" for the built-in preset
    # Off by default. A command connector runs on the host as the host user, outside
    # the sandbox; the operator must say so with allow_host_command = true.
    allow_host_command: bool = False

    @property
    def read_tools(self) -> list[str]:
        return [t.name for t in self.tools if t.mode == "read"]

    @property
    def write_tools(self) -> list[str]:
        return [t.name for t in self.tools if t.mode == "write"]

    def token(self, env: Mapping[str, str] | None = None) -> str | None:
        if self.auth != "bearer_env":
            return None
        source = os.environ if env is None else env
        return source.get(self.auth_env) or None


@dataclass(frozen=True)
class FactiqConfig:
    enabled: bool
    url: str
    auth: str  # "bearer_env" (a FactIQ API key) or "oauth"
    api_key_env: str
    instructions: bool  # share the plugin's public skill files with the model
    feedback: bool  # allow send_feedback, as an action that asks first


_GITHUB_REMOTE = (
    re.compile(r"^https://(?P<host>[^/@]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"),
    re.compile(
        r"^ssh://git@(?P<host>[^/:]+)(?::\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
    ),
    re.compile(r"^git@(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
)


def parse_github_remote(remote: str, host: str) -> tuple[str, str] | None:
    """Owner and repository name from an https, ssh:// or scp-style remote on host."""
    for pattern in _GITHUB_REMOTE:
        match = pattern.match(remote)
        if match and match["host"].lower() == host.lower():
            return match["owner"], match["repo"]
    return None


@dataclass(frozen=True)
class Config:
    core: CoreConfig
    limits: LimitsConfig
    worker_backend: BackendChoice
    codex: CodexConfig
    claude_code: ClaudeCodeConfig
    opencode: OpencodeConfig
    fake: FakeBackendConfig
    sandbox: SandboxConfig
    cli: CliChannelConfig
    slack: SlackChannelConfig
    source_path: Path | None = None
    github: GithubConfig | None = None
    repositories: list[RepositoryConfig] = field(default_factory=list)
    browser: BrowserConfig | None = None
    gateway: GatewayConfig | None = None
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    factiq: FactiqConfig | None = None
    # Keys from before 0.3 that the file still has, such as "reviewer"; ignored.
    ignored_keys: list[str] = field(default_factory=list)

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

    @property
    def repos_dir(self) -> Path:
        """Control clones, one per repository. Never mounted into a container."""
        return self.core.state_root / "repos"

    @property
    def worktrees_dir(self) -> Path:
        """Per-task copies: worktrees/task-<id>/<repository>."""
        return self.core.state_root / "worktrees"

    @property
    def connectors_dir(self) -> Path:
        """Per-connector files shared with the model read-only, such as instructions."""
        return self.core.state_root / "connectors"

    def ensure_directories(self) -> None:
        """Create the state root and its folders with mode 0700; tighten them if they exist."""
        for path in (
            self.state_root,
            self.runs_dir,
            self.logs_dir,
            self.repos_dir,
            self.worktrees_dir,
            self.connectors_dir,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def repository(self, name: str) -> RepositoryConfig | None:
        for repo in self.repositories:
            if repo.name == name:
                return repo
        return None

    def connectors(self) -> list[McpServerConfig]:
        """Every enabled MCP connector: the FactIQ preset first, then [[mcp_servers]]."""
        result = []
        if self.factiq is not None and self.factiq.enabled:
            result.append(factiq_connector(self.factiq, self.connectors_dir))
        result.extend(self.mcp_servers)
        return result

    def connector(self, name: str) -> McpServerConfig | None:
        for server in self.connectors():
            if server.name == name:
                return server
        return None

    def secret_env_names(self) -> set[str]:
        """Host variables that hold logins. None may enter a step container."""
        names = {self.slack.bot_token_env, self.claude_code.token_env}
        if self.github is not None:
            names.add(self.github.token_env)
        if self.factiq is not None:
            names.add(self.factiq.api_key_env)
        names.update(s.auth_env for s in self.mcp_servers if s.auth_env)
        return names

    def backend_choice(self, role: str) -> BackendChoice:
        if role == "worker":
            return self.worker_backend
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
        data, ignored = _drop_retired_keys(data)
        merged = copy.deepcopy(DEFAULTS)
        _merge(merged, data)
        for name, raw in (env or {}).items():
            if name in ENV_OVERRIDES:
                key_path = ENV_OVERRIDES[name]
                _set(merged, key_path, _parse_env_value(name, raw, _get(DEFAULTS, key_path)))
        return dataclasses.replace(_build(merged, source_path), ignored_keys=ignored)


def _drop_retired_keys(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A copy of data without RETIRED_KEYS, and the dotted names of the keys it had."""
    data = copy.deepcopy(dict(data))
    ignored = []
    for key_path in RETIRED_KEYS:
        parent: Any = data
        for part in key_path[:-1]:
            parent = parent.get(part) if isinstance(parent, Mapping) else None
        if isinstance(parent, dict) and key_path[-1] in parent:
            del parent[key_path[-1]]
            ignored.append(".".join(key_path))
    return data, ignored


def _is_provider_model(model: str) -> bool:
    provider, slash, name = model.partition("/")
    return bool(slash and provider and name)


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

    backend = data["backend"]
    kinds = known_backend_kinds()
    choices = {}
    for role in BACKEND_ROLES:
        kind = backend[role]["kind"]
        if kind not in kinds:
            raise ConfigError(f"backend.{role}.kind must be one of {sorted(kinds)}, got {kind!r}")
        model = backend[role]["model"]
        if kind == "opencode" and not _is_provider_model(model):
            raise ConfigError(
                f"backend.{role}.model must name a provider/model for opencode, "
                f"for example 'openrouter/anthropic/claude-sonnet-5.5'; got {model!r}"
            )
        choices[role] = BackendChoice(kind=kind, model=model)

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
    if not isinstance(sandbox["no_new_privileges"], bool):
        raise ConfigError("sandbox.no_new_privileges must be true or false")

    github = _build_github(data["github"])
    repositories = _build_repositories(data["repositories"])
    if any(repo.plain_git for repo in repositories) and not github.author_email:
        raise ConfigError(
            "github.author_email must be set when a repository has forge = 'none'; "
            "without GitHub there is no account to take a no-reply address from"
        )
    browser = _build_browser(data["browser"])
    gateway = data["gateway"]
    _positive(gateway["call_timeout_seconds"], "gateway.call_timeout_seconds")
    _positive(gateway["max_result_kib"], "gateway.max_result_kib")
    factiq = _build_factiq(data["factiq"])
    mcp_servers = _build_mcp_servers(data["mcp_servers"])
    if factiq.enabled and any(s.name == "factiq" for s in mcp_servers):
        raise ConfigError(
            "mcp_servers has an entry named factiq while factiq.enabled is true; remove one of them"
        )

    secret_names = {
        data["channels"]["slack"]["bot_token_env"],
        backend["claude_code"]["token_env"],
        github.token_env,
        factiq.api_key_env,
    }
    secret_names.update(s.auth_env for s in mcp_servers if s.auth_env)
    for name in sandbox["env_allowlist"]:
        if not isinstance(name, str) or not name:
            raise ConfigError("sandbox.env_allowlist must hold variable names")
        if name in secret_names:
            raise ConfigError(
                f"sandbox.env_allowlist may not name {name}, which holds a login the host keeps"
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
        worker_backend=choices["worker"],
        codex=CodexConfig(auth_file=_expand(backend["codex"]["auth_file"])),
        claude_code=ClaudeCodeConfig(
            token_env=backend["claude_code"]["token_env"],
            token_file=_expand(backend["claude_code"]["token_file"]),
        ),
        opencode=OpencodeConfig(auth_file=_expand(backend["opencode"]["auth_file"])),
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
            no_new_privileges=sandbox["no_new_privileges"],
        ),
        cli=CliChannelConfig(enabled=cli["enabled"], user=cli["user"]),
        slack=SlackChannelConfig(
            enabled=slack["enabled"],
            bot_token_env=slack["bot_token_env"],
            channels=list(slack["channels"]),
            allowed_users=list(slack["allowed_users"]),
        ),
        source_path=source_path,
        github=github,
        repositories=repositories,
        browser=browser,
        gateway=GatewayConfig(
            call_timeout_seconds=gateway["call_timeout_seconds"],
            max_result_kib=gateway["max_result_kib"],
        ),
        mcp_servers=mcp_servers,
        factiq=factiq,
    )


def _string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return list(value)


def _check_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.match(value) or "__" in value:
        raise ConfigError(
            f"{where} must use only letters, digits, '-' and '_', without '__'; got {value!r}"
        )
    return value


def _check_keys(item: Any, where: str, required: set[str], optional: set[str]) -> None:
    if not isinstance(item, Mapping):
        raise ConfigError(f"{where} must be a table")
    keys = set(item)
    if not required <= keys or not keys <= required | optional:
        raise ConfigError(
            f"{where} needs {sorted(required)} and may have {sorted(optional)}; got {sorted(keys)}"
        )


def _build_github(github: dict[str, Any]) -> GithubConfig:
    if not github["token_env"]:
        raise ConfigError("github.token_env must name a variable")
    if not github["api_url"].startswith("https://"):
        raise ConfigError("github.api_url must be an https:// address")
    if not github["branch_prefix"] or github["branch_prefix"].startswith(("/", "-")):
        raise ConfigError("github.branch_prefix must be a branch name prefix such as opendot/")
    _positive(github["max_file_kib"], "github.max_file_kib")
    _positive(github["check_minutes"], "github.check_minutes")
    _positive(github["max_diff_chars"], "github.max_diff_chars")
    if not isinstance(github["allow_binary_public"], bool):
        raise ConfigError("github.allow_binary_public must be true or false")
    return GithubConfig(
        token_env=github["token_env"],
        api_url=github["api_url"].rstrip("/"),
        host=github["host"],
        author_name=github["author_name"],
        author_email=github["author_email"],
        committer_name=github["committer_name"],
        committer_email=github["committer_email"],
        signing_key=_expand(github["signing_key"]) if github["signing_key"] else None,
        branch_prefix=github["branch_prefix"],
        private_markers=_string_list(github["private_markers"], "github.private_markers"),
        forbidden_files=_string_list(github["forbidden_files"], "github.forbidden_files"),
        max_file_kib=github["max_file_kib"],
        check_minutes=github["check_minutes"],
        max_diff_chars=github["max_diff_chars"],
        allow_binary_public=github["allow_binary_public"],
    )


def _build_repositories(items: list[Any]) -> list[RepositoryConfig]:
    repos = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        where = f"repositories[{index}]"
        _check_keys(
            item,
            where,
            {"name", "remote"},
            {
                "default_branch",
                "prepare",
                "checks",
                "public",
                "check_network",
                "forge",
                "visibility",
            },
        )
        name = _check_name(item["name"], f"{where}.name")
        if name in seen:
            raise ConfigError(f"{where}.name {name!r} is used twice")
        seen.add(name)
        remote = item["remote"]
        if not isinstance(remote, str) or not remote or remote.startswith("-"):
            raise ConfigError(f"{where}.remote must be a git remote address")
        public = item.get("public", False)
        if not isinstance(public, bool):
            raise ConfigError(f"{where}.public must be true or false")
        branch = item.get("default_branch", "main")
        if not isinstance(branch, str) or not branch or branch.startswith("-"):
            raise ConfigError(f"{where}.default_branch must be a branch name")
        network = item.get("check_network", "none")
        if not isinstance(network, str) or not network:
            raise ConfigError(f"{where}.check_network must name a Docker network, or 'none'")
        if network == "host" or network.startswith("container:"):
            raise ConfigError(f"{where}.check_network may not share another network namespace")
        forge = item.get("forge", "github")
        if forge not in ("github", "none"):
            raise ConfigError(f"{where}.forge must be 'github' or 'none'")
        visibility = item.get("visibility", "")
        if forge == "none" and visibility not in ("private", "public"):
            raise ConfigError(
                f"{where}.visibility must be 'private' or 'public' when forge = 'none', "
                "because no service reports it"
            )
        if forge == "github" and visibility != "":
            raise ConfigError(f"{where}.visibility is only for forge = 'none'; GitHub reports it")
        repos.append(
            RepositoryConfig(
                name=name,
                remote=remote,
                default_branch=branch,
                prepare=_string_list(item.get("prepare", []), f"{where}.prepare"),
                checks=_string_list(item.get("checks", []), f"{where}.checks"),
                public=public,
                check_network=network,
                forge=forge,
                visibility=visibility,
            )
        )
    return repos


def _build_browser(browser: dict[str, Any]) -> BrowserConfig:
    if not re.match(r"^\d{2,5}x\d{2,5}$", browser["viewport"]):
        raise ConfigError("browser.viewport must look like 1280x800")
    tools = _string_list(browser["tools"], "browser.tools")
    for tool in tools:
        if not NAME_PATTERN.match(tool):
            raise ConfigError(f"browser.tools has an invalid tool name {tool!r}")
    return BrowserConfig(
        enabled=browser["enabled"],
        viewport=browser["viewport"],
        allowed_origins=_string_list(browser["allowed_origins"], "browser.allowed_origins"),
        shm_size=browser["shm_size"],
        tools=tools,
    )


def _build_factiq(factiq: dict[str, Any]) -> FactiqConfig:
    if factiq["auth"] not in ("bearer_env", "oauth"):
        raise ConfigError("factiq.auth must be bearer_env or oauth")
    if not factiq["url"].startswith("https://"):
        raise ConfigError("factiq.url must be an https:// address")
    if factiq["auth"] == "bearer_env" and not factiq["api_key_env"]:
        raise ConfigError("factiq.api_key_env must name a variable")
    return FactiqConfig(
        enabled=factiq["enabled"],
        url=factiq["url"],
        auth=factiq["auth"],
        api_key_env=factiq["api_key_env"],
        instructions=factiq["instructions"],
        feedback=factiq["feedback"],
    )


def factiq_connector(factiq: FactiqConfig, connectors_dir: Path) -> McpServerConfig:
    """The FactIQ preset as an ordinary connector."""
    tools = [McpToolConfig(name=t, mode="read") for t in FACTIQ_READ_TOOLS]
    if factiq.feedback:
        tools.extend(McpToolConfig(name=t, mode="write") for t in FACTIQ_WRITE_TOOLS)
    return McpServerConfig(
        name="factiq",
        url=factiq.url,
        command=[],
        auth=factiq.auth,
        auth_env=factiq.api_key_env if factiq.auth == "bearer_env" else "",
        tools=tools,
        instructions=connectors_dir / "factiq" / "instructions" if factiq.instructions else None,
        preset="factiq",
    )


def _build_mcp_servers(items: list[Any]) -> list[McpServerConfig]:
    servers = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        where = f"mcp_servers[{index}]"
        _check_keys(
            item,
            where,
            {"name", "tools"},
            {"url", "command", "auth", "auth_env", "allow_host_command"},
        )
        name = _check_name(item["name"], f"{where}.name")
        if name in RESERVED_SERVER_NAMES:
            raise ConfigError(f"{where}.name {name!r} is reserved")
        if name in seen:
            raise ConfigError(f"{where}.name {name!r} is used twice")
        seen.add(name)
        url = item.get("url", "")
        command = item.get("command", [])
        if not isinstance(url, str):
            raise ConfigError(f"{where}.url must be a string")
        command = _string_list(command, f"{where}.command")
        if bool(url) == bool(command):
            raise ConfigError(f"{where} needs exactly one of url and command")
        if url:
            _check_connector_url(url, f"{where}.url")
        allow_host_command = item.get("allow_host_command", False)
        if not isinstance(allow_host_command, bool):
            raise ConfigError(f"{where}.allow_host_command must be true or false")
        if command and not allow_host_command:
            raise ConfigError(
                f"{where}.command runs {command[0]!r} on the host as your user, outside the "
                "sandbox, with your files and network. Set allow_host_command = true in this "
                "connector's table if you trust that program."
            )
        if allow_host_command and not command:
            raise ConfigError(f"{where}.allow_host_command is used only with command")
        auth = item.get("auth", "none")
        if auth not in MCP_AUTH_KINDS:
            raise ConfigError(f"{where}.auth must be one of {list(MCP_AUTH_KINDS)}")
        auth_env = item.get("auth_env", "")
        if not isinstance(auth_env, str):
            raise ConfigError(f"{where}.auth_env must be a variable name")
        if auth == "bearer_env" and not auth_env:
            raise ConfigError(f"{where}.auth_env is needed when auth = 'bearer_env'")
        if auth != "bearer_env" and auth_env:
            raise ConfigError(f"{where}.auth_env is used only when auth = 'bearer_env'")
        if auth == "oauth" and not url:
            raise ConfigError(f"{where}.auth = 'oauth' needs a url")
        if not isinstance(item["tools"], list) or not item["tools"]:
            raise ConfigError(f"{where}.tools must list at least one tool")
        tools = []
        tool_names: set[str] = set()
        for t_index, tool in enumerate(item["tools"]):
            t_where = f"{where}.tools[{t_index}]"
            _check_keys(tool, t_where, {"name", "mode"}, set())
            tool_name = _check_name(tool["name"], f"{t_where}.name")
            if tool_name in tool_names:
                raise ConfigError(f"{t_where}.name {tool_name!r} is listed twice")
            tool_names.add(tool_name)
            if tool["mode"] not in MCP_TOOL_MODES:
                raise ConfigError(f"{t_where}.mode must be read or write")
            tools.append(McpToolConfig(name=tool_name, mode=tool["mode"]))
        servers.append(
            McpServerConfig(
                name=name,
                url=url,
                command=command,
                auth=auth,
                auth_env=auth_env,
                tools=tools,
                allow_host_command=allow_host_command,
            )
        )
    return servers


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _check_connector_url(url: str, where: str) -> None:
    """https:// anywhere, or http:// to this machine only (by host name, not by prefix)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise ConfigError(f"{where} is not a valid address: {exc}") from None
    if not host:
        raise ConfigError(f"{where} has no host name")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and host.lower() in LOCAL_HOSTS:
        return
    raise ConfigError(
        f"{where} must be https://, or http:// to 127.0.0.1, localhost or [::1]; got {url!r}"
    )
