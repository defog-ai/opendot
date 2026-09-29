"""The Claude Code backend: `claude -p` with stream-json output, inside a step container.

One step is one `claude` process in print mode. The prompt goes in on stdin and
stdin is then closed. The step schema goes in with --json-schema, and the
structured output is read from the final "result" line of the stream.

Permissions: --permission-mode dontAsk refuses every tool that is not listed in
--allowedTools, so the CLI never waits for an answer. Work steps may use the
shell and file tools inside the container. Review and reflect steps get no
tools at all (--tools "").

Sessions: a new step picks its session id up front with --session-id, so its
host folder can be created before the container starts. A resumed step passes
--resume with the id from the earlier StepResult.

The login is the host variable named by backend.claude_code.token_env. Its value
reaches the container only through the environment of the docker process
(a bare `--env NAME`), never through the command line.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from opendot.redact import redact, write_redacted
from opendot.sandbox import (
    CONTAINER_CLI_HOME,
    SandboxError,
    adopt_session,
    check_host_mounts,
    check_mounts,
    check_plan_env,
    check_step_env,
    claude_mcp_allowed_tools,
    claude_mcp_config,
    container_name,
    docker_run_args,
    new_session,
    open_session,
    remove_session,
    stop_container,
)

if TYPE_CHECKING:
    from opendot.config import Config

KIND = "claude_code"

# Tools a work step may use. Everything else is refused by --permission-mode dontAsk.
WORK_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")

OAUTH_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_VARIABLE = "ANTHROPIC_API_KEY"


def container_token_variable(token_env: str, value: str) -> str:
    """The variable name the CLI reads the login from inside the container.

    An Anthropic API key goes in ANTHROPIC_API_KEY; anything else is treated as a
    Claude Code OAuth token (from `claude setup-token`).
    """
    if token_env == API_KEY_VARIABLE or value.startswith("sk-ant-api"):
        return API_KEY_VARIABLE
    return OAUTH_TOKEN_VARIABLE


def claude_args(
    step: Step,
    output_schema: dict[str, Any],
    *,
    session_id: str,
    resume: bool,
    model: str = "",
    mcp_servers: Sequence[McpServerSpec] = (),
) -> list[str]:
    """The `claude` command line run inside the container. The prompt is not in it.

    The "$schema" key is left out of the schema: the CLI refuses a --json-schema
    that names its dialect. mcp_servers (work steps only) are given with
    --mcp-config, and only their listed tools are added to --allowedTools.
    """
    schema = {key: value for key, value in output_schema.items() if key != "$schema"}
    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--permission-mode",
        "dontAsk",
        "--strict-mcp-config",
    ]
    if model:
        args += ["--model", model]
    if resume:
        args += ["--resume", session_id]
    else:
        args += ["--session-id", session_id]
    if Step(step) is Step.WORK:
        allowed = list(WORK_TOOLS)
        if mcp_servers:
            config = claude_mcp_config(mcp_servers)
            args += ["--mcp-config", json.dumps(config, separators=(",", ":"))]
            allowed += claude_mcp_allowed_tools(mcp_servers)
        args += ["--allowedTools", ",".join(allowed)]
    else:
        if mcp_servers:
            raise SandboxError(f"a {Step(step).value} step gets no MCP servers")
        args += ["--tools", ""]
    return args


@dataclass
class _StreamOutcome:
    result: dict[str, Any] | None
    session_id: str | None
    lines: list[str]


class ClaudeCodeBackend:
    kind = KIND

    def __init__(
        self,
        config: Config,
        role: str,
        runner: CommandRunner | None = None,
        *,
        host_env: Mapping[str, str] | None = None,
        poll_seconds: float = 0.5,
        exit_grace_seconds: float = 10.0,
    ):
        self.config = config
        self.role = role
        self.model = config.backend_choice(role).model
        self.runner = runner or SubprocessRunner()
        self.host_env = os.environ if host_env is None else host_env
        self.poll_seconds = poll_seconds
        self.exit_grace_seconds = exit_grace_seconds

    @classmethod
    def from_config(
        cls, config: Config, role: str, runner: CommandRunner | None = None
    ) -> ClaudeCodeBackend:
        return cls(config, role, runner)

    def _login(self) -> tuple[str, str]:
        token_env = self.config.claude_code.token_env
        value = self.host_env.get(token_env, "")
        if not value:
            raise BackendError(
                f"{token_env} is not set; run `claude setup-token` on the host and export it"
            )
        return container_token_variable(token_env, value), value

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
        token_variable, token = self._login()
        try:
            env = check_step_env(step, env, self.config)
            mounts = check_mounts(step, mounts, self.config)
            host_mounts = check_host_mounts(step, plan.host_mounts, self.config) if plan else []
            plan_env = check_plan_env(plan.fixed_env) if plan else {}
            mcp_servers = list(plan.mcp_servers) if plan else []
            if mcp_servers and step is not Step.WORK:
                raise SandboxError(f"a {step.value} step gets no MCP servers")
            claude_mcp_config(mcp_servers)  # raises SandboxError for a bad name
            if resume_id:
                session = open_session(self.config, KIND, resume_id)
                session_id = resume_id
            else:
                session_id = str(uuid.uuid4())
                session = new_session(self.config, KIND, session_id)
        except SandboxError as exc:
            raise BackendError(str(exc)) from exc

        if should_stop is not None and should_stop():
            if not resume_id:
                remove_session(session)
            raise StepInterrupted(f"the {step.value} step was stopped before it started")

        timeout = limits.timeout_seconds if limits else None
        if timeout is None and self.config.limits.step_minutes:
            timeout = self.config.limits.step_minutes * 60.0

        secrets = [token, *env.values()]
        name = container_name(KIND, step)
        args = docker_run_args(
            self.config.sandbox,
            name=name,
            command=claude_args(
                step,
                output_schema,
                session_id=session_id,
                resume=bool(resume_id),
                model=self.model,
                mcp_servers=mcp_servers,
            ),
            session=session,
            work_writable=step is not Step.REVIEW,
            mounts=mounts,
            env_names=sorted([*env, token_variable]),
            fixed_env={**plan_env, "CLAUDE_CONFIG_DIR": CONTAINER_CLI_HOME},
            host_mounts=host_mounts,
            shm_size=plan.shm_size if plan else "",
        )
        process_env = {**env, token_variable: token}

        outcome = _StreamOutcome(result=None, session_id=None, lines=[])
        stderr = ""
        succeeded = False
        try:
            process = self.runner.spawn(args, env=process_env)
            try:
                self._send_prompt(process, prompt)
                stderr_reader = LineReader(process.stderr) if process.stderr is not None else None
                self._read_stream(process, outcome, Deadline(timeout), should_stop)
                self._wait_or_kill(process, name)
                stderr = _drain(stderr_reader)
            except BaseException:
                self._kill(process, name)
                raise
            output, usage = _parse_result(outcome.result, process.returncode, stderr)
            thread_id = outcome.result.get("session_id") or outcome.session_id or session_id
            if not resume_id and thread_id != session_id:
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
            if not succeeded and not resume_id:
                remove_session(session)
            transcript_path = self._write_transcript(name, outcome.lines, secrets)

        return StepResult(
            output=output, thread_id=thread_id, transcript_path=transcript_path, usage=usage
        )

    def _send_prompt(self, process: Process, prompt: str) -> None:
        if process.stdin is None:
            raise BackendError("the claude process was started without an input pipe")
        try:
            process.stdin.write(prompt)
            process.stdin.flush()
        except (OSError, ValueError):
            # The process exited early. Keep reading: the missing result line is
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
            raise BackendError("the claude process was started without an output pipe")
        reader = LineReader(process.stdout)
        while True:
            if deadline.passed:
                raise StepTimedOut("the Claude Code step ran past its time limit")
            if should_stop is not None and should_stop():
                raise StepInterrupted("the Claude Code step was stopped")
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
                message = json.loads(text)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "system" and message.get("subtype") == "init":
                if isinstance(message.get("session_id"), str):
                    outcome.session_id = message["session_id"]
            elif message.get("type") == "result":
                outcome.result = message

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


def _parse_result(
    result: dict[str, Any] | None, returncode: int | None, stderr: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if result is None:
        detail = f": {stderr}" if stderr else ""
        raise BackendError(f"claude exited with {returncode} before it reported a result{detail}")
    if result.get("is_error") or result.get("subtype") != "success":
        reason = result.get("result") or result.get("subtype") or "unknown error"
        raise BackendError(f"the Claude Code step failed: {reason}")
    output = result.get("structured_output")
    if output is None:
        try:
            output = json.loads(result.get("result") or "")
        except ValueError as exc:
            raise BackendError("the Claude Code result is not JSON") from exc
    if not isinstance(output, dict):
        raise BackendError("the Claude Code result is not a JSON object")
    usage: dict[str, Any] = {}
    if isinstance(result.get("num_turns"), int):
        usage["turns"] = result["num_turns"]
    tokens = result.get("usage")
    if isinstance(tokens, dict):
        usage["input_tokens"] = sum(
            int(tokens.get(key) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        usage["output_tokens"] = int(tokens.get("output_tokens") or 0)
    return output, usage
