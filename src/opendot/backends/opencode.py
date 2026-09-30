"""The opencode backend: `opencode run --format json`, inside a step container.

One step is one `opencode run` process. The prompt goes in on stdin and stdin is
then closed. opencode has no flag for an output schema, so the schema is added
to the end of the prompt and the step's output is the last JSON object in the
model's final text. The host validates it against the schema, as for every
backend.

opencode prints one JSON event per line: step_start, text, tool_use and
step_finish parts, and an error event when the provider call fails. Every event
carries the session id.

Permissions: opencode reads its settings from OPENCODE_CONFIG_CONTENT. Work steps
allow every tool inside the container; reflect steps deny every tool.
Sharing and self-update are turned off, and --pure skips external plugins.

MCP servers (work steps only) go in the same settings under "mcp". opencode names
a server's tools <server>_<tool>. For a server with a tool list, the settings deny
<server>_* and then allow each listed tool; opencode uses the last rule that
matches, so the other tools are hidden from the model.

Sessions: opencode keeps its sessions in a SQLite file under XDG_DATA_HOME, which
points into the session's CLI folder, so a later step can resume with --session.

Login: opencode keeps logins for many providers in one file (auth.json). Only the
entry for the provider of the configured model (the part before the first "/")
is copied into the session's CLI folder, for the length of the step. If opencode
refreshed an OAuth login during the step, the new entry is written back to the
host file so the host copy stays valid.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opendot.backends import (
    BackendError,
    CommandRunner,
    Process,
    StepInterrupted,
    StepLimits,
    StepTimedOut,
    SubprocessRunner,
)
from opendot.models import McpServerSpec, Mount, Step, StepPlan, StepResult
from opendot.proc import Deadline, LineReader
from opendot.redact import redact, secret_strings, write_redacted
from opendot.sandbox import (
    CONTAINER_CLI_HOME,
    CONTAINER_HOME,
    CONTAINER_WORKDIR,
    SandboxError,
    SessionDirs,
    adopt_session,
    check_host_mounts,
    check_mounts,
    check_plan_env,
    check_step_env,
    claude_mcp_config,
    container_name,
    docker_run_args,
    hand_to_container,
    new_session,
    open_session,
    remove_session,
    stop_container,
)

if TYPE_CHECKING:
    from opendot.config import Config

KIND = "opencode"

# The login file inside the session's CLI folder: XDG_DATA_HOME/opencode/auth.json.
LOGIN_FILE = Path("data") / "opencode" / "auth.json"

# XDG folders for the CLI. data/ and state/ hold the sessions and must survive the
# step; the cache is rebuilt in the tmpfs HOME each time.
XDG_ENV = {
    "XDG_DATA_HOME": f"{CONTAINER_CLI_HOME}/data",
    "XDG_CONFIG_HOME": f"{CONTAINER_CLI_HOME}/config",
    "XDG_STATE_HOME": f"{CONTAINER_CLI_HOME}/state",
    "XDG_CACHE_HOME": f"{CONTAINER_HOME}/.cache",
}


def provider_of(model: str) -> str:
    """The provider part of a "provider/model" name. Raises BackendError when absent."""
    provider, slash, name = model.partition("/")
    if not slash or not provider or not name:
        raise BackendError(
            f"the opencode backend needs a model named provider/model, got {model!r}; "
            "run `opencode models` to list them"
        )
    return provider


def opencode_settings(step: Step, mcp_servers: Sequence[McpServerSpec] = ()) -> dict[str, Any]:
    """The settings passed in OPENCODE_CONFIG_CONTENT for one step."""
    step = Step(step)
    if mcp_servers and step is not Step.WORK:
        raise BackendError(f"a {step.value} step gets no MCP servers")
    level = "allow" if step is Step.WORK else "deny"
    settings: dict[str, Any] = {
        "permission": {"*": level, **opencode_mcp_permissions(mcp_servers)},
        "share": "disabled",
        "autoupdate": False,
    }
    if mcp_servers:
        settings["mcp"] = opencode_mcp_config(mcp_servers)
    return settings


def opencode_mcp_config(specs: Sequence[McpServerSpec]) -> dict[str, Any]:
    """The "mcp" settings: one local (stdio) server per spec."""
    try:
        claude_mcp_config(specs)  # the same name, command and env checks
    except SandboxError as exc:
        raise BackendError(str(exc)) from exc
    names = [spec.name for spec in specs]
    for name in names:
        for other in names:
            if other != name and other.startswith(f"{name}_"):
                raise BackendError(
                    f"MCP server names {name!r} and {other!r} overlap in opencode's tool "
                    "names (<server>_<tool>); rename one"
                )
    servers: dict[str, Any] = {}
    for spec in specs:
        entry: dict[str, Any] = {
            "type": "local",
            "command": list(spec.command),
            "enabled": True,
            "timeout": int(spec.tool_timeout_seconds * 1000),
        }
        if spec.env:
            entry["environment"] = dict(spec.env)
        servers[spec.name] = entry
    return servers


def opencode_mcp_permissions(specs: Sequence[McpServerSpec]) -> dict[str, str]:
    """Rules that hide every tool a server's tool list leaves out. The order matters:
    opencode applies the last rule that matches a tool name."""
    rules: dict[str, str] = {}
    for spec in specs:
        if spec.tools:
            rules[f"{spec.name}_*"] = "deny"
    for spec in specs:
        for tool in spec.tools:
            rules[f"{spec.name}_{tool}"] = "allow"
    return rules


def opencode_args(model: str, session_id: str | None = None) -> list[str]:
    """The `opencode` command line run inside the container. The prompt is not in it."""
    args = [
        "opencode",
        "run",
        "--format",
        "json",
        "--pure",
        "--model",
        model,
        "--dir",
        CONTAINER_WORKDIR,
    ]
    if session_id:
        args += ["--session", session_id]
    return args


def prompt_with_schema(prompt: str, output_schema: dict[str, Any]) -> str:
    """The prompt plus the output rule, because opencode takes no schema flag."""
    schema = json.dumps(output_schema, indent=2)
    return (
        f"{prompt.rstrip()}\n\n"
        "## Output format\n\n"
        "End your final message with one JSON object that follows this JSON schema. "
        "Write nothing after it.\n\n"
        f"```json\n{schema}\n```\n"
    )


def last_json_object(text: str) -> dict[str, Any] | None:
    """The last JSON object in text, with or without a code fence around it."""
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found = value
        index = text.find("{", end)
    return found


@dataclass
class _StreamOutcome:
    session_id: str | None = None
    texts: list[str] = field(default_factory=list)  # text parts of the latest model step
    error: str | None = None
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    lines: list[str] = field(default_factory=list)


@dataclass
class _Login:
    provider: str
    entry: dict[str, Any]
    secrets: list[str]


class OpencodeBackend:
    kind = KIND

    def __init__(
        self,
        config: Config,
        role: str,
        runner: CommandRunner | None = None,
        *,
        poll_seconds: float = 0.5,
        exit_grace_seconds: float = 10.0,
    ):
        self.config = config
        self.role = role
        self.model = config.backend_choice(role).model
        self.runner = runner or SubprocessRunner()
        self.poll_seconds = poll_seconds
        self.exit_grace_seconds = exit_grace_seconds

    @classmethod
    def from_config(
        cls, config: Config, role: str, runner: CommandRunner | None = None
    ) -> OpencodeBackend:
        return cls(config, role, runner)

    # -- login ---------------------------------------------------------------

    def _read_login(self) -> _Login:
        provider = provider_of(self.model)
        source = self.config.opencode.auth_file.expanduser()
        try:
            logins = json.loads(source.read_bytes())
        except OSError as exc:
            raise BackendError(
                f"the opencode login file {source} is missing; run `opencode auth login`"
            ) from exc
        except ValueError as exc:
            raise BackendError(f"the opencode login file {source} is not JSON") from exc
        entry = logins.get(provider) if isinstance(logins, dict) else None
        if not isinstance(entry, dict):
            raise BackendError(
                f"the opencode login file has no login for {provider!r}; "
                f"run `opencode auth login` and choose {provider}"
            )
        return _Login(provider, entry, secret_strings(entry))

    def _stage_login(self, session: SessionDirs, login: _Login) -> Path:
        staged = session.cli / LOGIN_FILE
        for folder in (staged.parent.parent, staged.parent):
            folder.mkdir(mode=0o700, exist_ok=True)
            hand_to_container(folder)
        content = json.dumps({login.provider: login.entry}).encode()
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        hand_to_container(staged)
        return staged

    def _unstage_login(self, staged: Path, login: _Login) -> list[str]:
        """Delete the staged login. If opencode refreshed it, copy it back to the host.

        Returns the secret strings of the refreshed entry, if any."""
        refreshed: list[str] = []
        try:
            if staged.is_file() and not staged.is_symlink():
                current = json.loads(staged.read_bytes())
                entry = current.get(login.provider) if isinstance(current, dict) else None
                if (
                    isinstance(entry, dict)
                    and entry != login.entry
                    and _same_login(login.entry, entry)
                ):
                    host_file = self.config.opencode.auth_file.expanduser()
                    host = json.loads(host_file.read_bytes())
                    if isinstance(host, dict) and host.get(login.provider) == login.entry:
                        refreshed = secret_strings(entry)
                        host[login.provider] = entry
                        _replace_file(host_file, json.dumps(host, indent=2).encode())
        except (OSError, ValueError):
            pass
        finally:
            staged.unlink(missing_ok=True)
        return refreshed

    # -- Backend -------------------------------------------------------------

    def run_step(
        self,
        step: Step,
        prompt: str,
        output_schema: dict[str, Any],
        env: Mapping[str, str],
        mounts: Sequence[Mount],
        resume_id: str | None = None,
        *,
        limits: StepLimits | None = None,
        should_stop: Callable[[], bool] | None = None,
        plan: StepPlan | None = None,
    ) -> StepResult:
        step = Step(step)
        login = self._read_login()
        try:
            env = check_step_env(step, env, self.config)
            mounts = check_mounts(step, mounts, self.config)
            host_mounts = check_host_mounts(step, plan.host_mounts, self.config) if plan else []
            plan_env = check_plan_env(plan.fixed_env) if plan else {}
            settings = opencode_settings(step, list(plan.mcp_servers) if plan else [])
            session = (
                open_session(self.config, KIND, resume_id)
                if resume_id
                else new_session(self.config, KIND)
            )
        except SandboxError as exc:
            raise BackendError(str(exc)) from exc

        if should_stop is not None and should_stop():
            if not resume_id:
                remove_session(session)
            raise StepInterrupted(f"the {step.value} step was stopped before it started")

        timeout = limits.timeout_seconds if limits else None
        if timeout is None and self.config.limits.step_minutes:
            timeout = self.config.limits.step_minutes * 60.0

        secrets = [*login.secrets, *env.values()]
        name = container_name(KIND, step)
        args = docker_run_args(
            self.config.sandbox,
            name=name,
            command=opencode_args(self.model, resume_id),
            session=session,
            work_writable=True,
            mounts=mounts,
            env_names=sorted(env),
            fixed_env={
                **plan_env,
                **XDG_ENV,
                "OPENCODE_CONFIG_CONTENT": json.dumps(settings, separators=(",", ":")),
            },
            host_mounts=host_mounts,
            shm_size=plan.shm_size if plan else "",
        )

        outcome = _StreamOutcome()
        stderr = ""
        succeeded = False
        staged: Path | None = None
        try:
            staged = self._stage_login(session, login)
            process = self.runner.spawn(args, env=dict(env))
            try:
                self._send_prompt(process, prompt_with_schema(prompt, output_schema))
                stderr_reader = LineReader(process.stderr) if process.stderr is not None else None
                self._read_stream(process, outcome, Deadline(timeout), should_stop)
                self._wait_or_kill(process, name)
                stderr = _drain(stderr_reader)
            except BaseException:
                self._kill(process, name)
                raise
            finally:
                secrets += self._unstage_login(staged, login)
                staged = None
            output = _parse_output(outcome, process.returncode, stderr)
            thread_id = outcome.session_id or resume_id
            if not thread_id:
                raise BackendError("opencode did not report a session id")
            if not resume_id:
                try:
                    session = adopt_session(session, self.config, KIND, thread_id)
                except SandboxError as exc:
                    raise BackendError(str(exc)) from exc
            succeeded = True
        except BackendError as exc:
            if isinstance(exc, StepInterrupted | StepTimedOut):
                raise
            raise BackendError(redact(str(exc), secrets)) from exc
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
            if not succeeded and not resume_id:
                remove_session(session)
            transcript_path = self._write_transcript(name, outcome.lines, secrets)

        usage = {
            "turns": outcome.turns,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
        }
        return StepResult(
            output=output, thread_id=thread_id, transcript_path=transcript_path, usage=usage
        )

    def _send_prompt(self, process: Process, prompt: str) -> None:
        if process.stdin is None:
            raise BackendError("the opencode process was started without an input pipe")
        try:
            process.stdin.write(prompt)
            process.stdin.flush()
        except (OSError, ValueError):
            # The process exited early. Keep reading: the missing answer is
            # reported together with its stderr.
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def _read_stream(
        self,
        process: Process,
        outcome: _StreamOutcome,
        deadline: Deadline,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        if process.stdout is None:
            raise BackendError("the opencode process was started without an output pipe")
        reader = LineReader(process.stdout)
        while True:
            if deadline.passed:
                raise StepTimedOut("the opencode step ran past its time limit")
            if should_stop is not None and should_stop():
                raise StepInterrupted("the opencode step was stopped")
            wait = self.poll_seconds
            remaining = deadline.remaining()
            if remaining is not None:
                wait = min(wait, max(remaining, 0.0))
            line = reader.get(wait)
            if line is None:
                continue
            if line is LineReader.EOF:
                return
            text = str(line).strip()
            if not text:
                continue
            outcome.lines.append(text)
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if isinstance(event, dict):
                _read_event(event, outcome)

    def _kill(self, process: Process, name: str) -> None:
        try:
            process.kill()
        except OSError:
            pass
        stop_container(self.runner, self.config.sandbox.docker, name)

    def _wait_or_kill(self, process: Process, name: str) -> None:
        try:
            process.wait(timeout=self.exit_grace_seconds)
        except subprocess.TimeoutExpired:
            self._kill(process, name)

    def _write_transcript(self, name: str, lines: list[str], secrets: list[str]) -> Path | None:
        if not lines:
            return None
        try:
            return write_redacted(
                self.config.runs_dir,
                Path("transcripts") / f"{name}.jsonl",
                "\n".join(lines) + "\n",
                secrets,
            )
        except OSError:
            return None


def _read_event(event: dict[str, Any], outcome: _StreamOutcome) -> None:
    if isinstance(event.get("sessionID"), str) and outcome.session_id is None:
        outcome.session_id = event["sessionID"]
    kind = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if kind == "step_start":
        outcome.texts = []
    elif kind == "text" and isinstance(part.get("text"), str):
        outcome.texts.append(part["text"])
    elif kind == "step_finish":
        outcome.turns += 1
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            outcome.input_tokens += (
                _int(tokens.get("input")) + _int(cache.get("read")) + _int(cache.get("write"))
            )
            outcome.output_tokens += _int(tokens.get("output")) + _int(tokens.get("reasoning"))
    elif kind == "error":
        error = event.get("error")
        message = None
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict):
                message = data.get("message")
            message = message or error.get("name")
        outcome.error = str(message or "unknown error")


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _parse_output(outcome: _StreamOutcome, returncode: int | None, stderr: str) -> dict[str, Any]:
    if outcome.error:
        raise BackendError(f"the opencode step failed: {outcome.error}")
    text = "".join(outcome.texts)
    if not text.strip():
        detail = f": {stderr}" if stderr else ""
        raise BackendError(f"opencode exited with {returncode} before it gave an answer{detail}")
    output = last_json_object(text)
    if output is None:
        raise BackendError("the opencode answer does not end with a JSON object")
    return output


def _drain(reader: LineReader | None) -> str:
    if reader is None:
        return ""
    parts: list[str] = []
    while True:
        line = reader.get(1.0)
        if line is None or line is LineReader.EOF:
            break
        parts.append(str(line))
    return "".join(parts)[-2000:].strip()


def _same_login(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when the entry the step left behind is a token refresh of the same login.

    Code in the container can write the file, so only an OAuth entry that keeps
    the same keys and the same type is copied back. API-key entries never change
    in a refresh, so a changed one is dropped."""
    return (
        before.get("type") == "oauth" and after.get("type") == "oauth" and set(before) == set(after)
    )


def _replace_file(path: Path, content: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
