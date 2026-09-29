"""The Codex backend: `codex app-server` over stdio, inside a step container.

The app server speaks JSON-RPC with one JSON object per line and no "jsonrpc"
field. One step is:

    initialize            -> result
    initialized           (notification)
    thread/start          -> result.thread.id        (new session)
      or thread/resume    -> result.thread.id        (resume_id given)
    turn/start            -> result.turn.id, with the step schema as outputSchema
    ... notifications and server requests ...
    turn/completed        turn.status is completed, interrupted or failed

The step's output is the text of the last agentMessage item of the turn, parsed
as JSON. A stop request sends turn/interrupt; a time-out kills the container.

The host answers the server's requests itself. The container is the security
boundary, so command and file-change approvals are accepted, questions for the
user get empty answers, MCP elicitations are declined, and every other request
gets an error.

Only the Codex login file (auth.json) is copied into the session's CLI folder,
for the length of the step. If Codex refreshed the login during the step, the
new file is written back to the host so the host copy stays valid.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opendot import __version__
from opendot.backends import (
    BackendError,
    CommandRunner,
    Process,
    StepInterrupted,
    StepLimits,
    StepTimedOut,
    SubprocessRunner,
)
from opendot.models import Mount, Step, StepPlan, StepResult
from opendot.proc import Deadline, LineReader
from opendot.redact import redact, secret_strings, write_redacted
from opendot.sandbox import (
    CONTAINER_CLI_HOME,
    CONTAINER_WORKDIR,
    SandboxError,
    SessionDirs,
    adopt_session,
    check_host_mounts,
    check_mounts,
    check_plan_env,
    check_step_env,
    codex_mcp_overrides,
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

KIND = "codex"
LOGIN_FILE = "auth.json"
APP_SERVER_COMMAND = ("codex", "app-server")

# Answers to the requests the app server sends the client, by method.
SERVER_REQUEST_ANSWERS: dict[str, dict[str, Any]] = {
    "item/commandExecution/requestApproval": {"decision": "accept"},
    "item/fileChange/requestApproval": {"decision": "accept"},
    "execCommandApproval": {"decision": "approved"},
    "applyPatchApproval": {"decision": "approved"},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
}


@dataclass
class _StepState:
    session: SessionDirs
    thread_id: str | None


class _Transcript:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, direction: str, message: Any) -> None:
        self.lines.append(json.dumps({"dir": direction, "msg": message}, ensure_ascii=False))


class CodexBackend:
    kind = KIND

    def __init__(
        self,
        config: Config,
        role: str,
        runner: CommandRunner | None = None,
        *,
        poll_seconds: float = 0.5,
        interrupt_grace_seconds: float = 15.0,
        exit_grace_seconds: float = 10.0,
    ):
        self.config = config
        self.role = role
        self.model = config.backend_choice(role).model
        self.runner = runner or SubprocessRunner()
        self.poll_seconds = poll_seconds
        self.interrupt_grace_seconds = interrupt_grace_seconds
        self.exit_grace_seconds = exit_grace_seconds

    @classmethod
    def from_config(
        cls, config: Config, role: str, runner: CommandRunner | None = None
    ) -> CodexBackend:
        return cls(config, role, runner)

    # -- login ---------------------------------------------------------------

    def _stage_login(self, session: SessionDirs) -> tuple[bytes, list[str]]:
        source = self.config.codex.auth_file.expanduser()
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise BackendError(
                f"the Codex login file {source} is missing; run `codex login` on the host"
            ) from exc
        try:
            secrets = secret_strings(json.loads(content))
        except ValueError:
            secrets = []
        staged = session.cli / LOGIN_FILE
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        hand_to_container(staged)
        return content, secrets

    def _unstage_login(self, staged: Path, original: bytes) -> list[str]:
        """Delete the staged login. If Codex refreshed it, copy the new one to the host.

        Returns the secret strings of the refreshed file, if any."""
        refreshed: list[str] = []
        try:
            if staged.is_file() and not staged.is_symlink():
                current = staged.read_bytes()
                host_file = self.config.codex.auth_file.expanduser()
                if (
                    current != original
                    and _same_account(original, current)
                    and host_file.read_bytes() == original
                ):
                    refreshed = secret_strings(json.loads(current))
                    _replace_file(host_file, current)
        except OSError:
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
        try:
            env = check_step_env(step, env, self.config)
            mounts = check_mounts(step, mounts, self.config)
            host_mounts = check_host_mounts(step, plan.host_mounts, self.config) if plan else []
            plan_env = check_plan_env(plan.fixed_env) if plan else {}
            mcp_args = codex_mcp_overrides(plan.mcp_servers) if plan else []
            session = (
                open_session(self.config, KIND, resume_id)
                if resume_id
                else new_session(self.config, KIND)
            )
        except SandboxError as exc:
            raise BackendError(str(exc)) from exc

        state = _StepState(session=session, thread_id=resume_id)
        if should_stop is not None and should_stop():
            if not resume_id:
                remove_session(session)
            raise StepInterrupted(f"the {step.value} step was stopped before it started")

        timeout = limits.timeout_seconds if limits else None
        if timeout is None and self.config.limits.step_minutes:
            timeout = self.config.limits.step_minutes * 60.0

        try:
            original_login, secrets = self._stage_login(session)
        except BackendError:
            if not resume_id:
                remove_session(session)
            raise
        secrets += list(env.values())
        name = container_name(KIND, step)
        args = docker_run_args(
            self.config.sandbox,
            name=name,
            command=[*APP_SERVER_COMMAND, *mcp_args],
            session=session,
            work_writable=step is not Step.REVIEW,
            mounts=mounts,
            env_names=sorted(env),
            fixed_env={**plan_env, "CODEX_HOME": CONTAINER_CLI_HOME},
            host_mounts=host_mounts,
            shm_size=plan.shm_size if plan else "",
        )
        transcript = _Transcript()
        client: _AppServerClient | None = None
        succeeded = False
        try:
            process = self.runner.spawn(args, env=env)
            try:
                client = _AppServerClient(
                    process,
                    transcript,
                    deadline=Deadline(timeout),
                    poll_seconds=self.poll_seconds,
                    interrupt_grace_seconds=self.interrupt_grace_seconds,
                    should_stop=should_stop,
                )
                output, usage = self._drive(client, step, prompt, output_schema, state)
            except BaseException:
                self._kill(process, name)
                raise
            finally:
                _close_quietly(process.stdin)
            self._wait_or_kill(process, name)
            succeeded = True
        except BackendError as exc:
            stderr = client.stderr_tail() if client is not None else ""
            if stderr and not isinstance(exc, StepInterrupted | StepTimedOut):
                raise BackendError(redact(f"{exc}\n{stderr}", secrets)) from exc
            raise
        finally:
            secrets += self._unstage_login(state.session.cli / LOGIN_FILE, original_login)
            if not succeeded and not resume_id:
                remove_session(state.session)
            transcript_path = self._write_transcript(name, transcript, secrets)

        return StepResult(
            output=output,
            thread_id=state.thread_id,
            transcript_path=transcript_path,
            usage=usage,
        )

    def _drive(
        self,
        client: _AppServerClient,
        step: Step,
        prompt: str,
        output_schema: dict[str, Any],
        state: _StepState,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        client.request(
            "initialize",
            {"clientInfo": {"name": "opendot", "title": "OpenDot", "version": __version__}},
        )
        client.notify("initialized")

        thread_params: dict[str, Any] = {
            "cwd": CONTAINER_WORKDIR,
            "approvalPolicy": "never",
            # The container is the sandbox; Codex's own sandbox needs kernel
            # features that an unprivileged container does not have.
            "sandbox": "danger-full-access",
        }
        if self.model:
            thread_params["model"] = self.model
        resume_id = state.thread_id
        if resume_id:
            result = client.request("thread/resume", {"threadId": resume_id, **thread_params})
        else:
            result = client.request("thread/start", {"ephemeral": False, **thread_params})
        thread_id = _dig(result, "thread", "id")
        if not isinstance(thread_id, str) or not thread_id:
            raise BackendError("the Codex app server did not return a thread id")
        if resume_id and thread_id != resume_id:
            raise BackendError(f"Codex resumed thread {thread_id} instead of {resume_id}")
        if not resume_id:
            try:
                state.session = adopt_session(state.session, self.config, KIND, thread_id)
            except SandboxError as exc:
                raise BackendError(str(exc)) from exc
        state.thread_id = thread_id
        client.thread_id = thread_id

        result = client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "outputSchema": output_schema,
            },
        )
        turn_id = _dig(result, "turn", "id")
        if isinstance(turn_id, str):
            client.turn_id = turn_id
        turn = client.wait_for_turn()

        status = turn.get("status")
        if status == "interrupted":
            raise StepInterrupted(f"the {step.value} step was stopped")
        if status != "completed":
            message = _dig(turn, "error", "message") or client.last_error or "no reason given"
            raise BackendError(f"the Codex turn {status or 'ended'}: {message}")
        text = client.final_message
        if text is None:
            raise BackendError("the Codex turn completed without a final message")
        try:
            output = json.loads(text)
        except ValueError as exc:
            raise BackendError("the final Codex message is not JSON") from exc
        if not isinstance(output, dict):
            raise BackendError("the final Codex message is not a JSON object")
        return output, client.usage()

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

    def _write_transcript(
        self, name: str, transcript: _Transcript, secrets: list[str]
    ) -> Path | None:
        if not transcript.lines:
            return None
        try:
            return write_redacted(
                self.config.runs_dir,
                Path("transcripts") / f"{name}.jsonl",
                "\n".join(transcript.lines) + "\n",
                secrets,
            )
        except OSError:
            return None


class _AppServerClient:
    """Reads and writes app-server messages for one turn."""

    def __init__(
        self,
        process: Process,
        transcript: _Transcript,
        *,
        deadline: Deadline,
        poll_seconds: float,
        interrupt_grace_seconds: float,
        should_stop: Callable[[], bool] | None,
    ):
        if process.stdin is None or process.stdout is None:
            raise BackendError("the Codex app server was started without pipes")
        self.process = process
        self.transcript = transcript
        self.deadline = deadline
        self.poll_seconds = poll_seconds
        self.interrupt_grace_seconds = interrupt_grace_seconds
        self.should_stop = should_stop
        self.reader = LineReader(process.stdout)
        self.stderr_reader = LineReader(process.stderr) if process.stderr is not None else None
        self._stderr: list[str] = []
        self._next_id = 0
        self._responses: dict[int, dict[str, Any]] = {}
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.final_message: str | None = None
        self.last_error: str | None = None
        self._completed_turn: dict[str, Any] | None = None
        self._first_total: dict[str, Any] | None = None
        self._first_last: dict[str, Any] | None = None
        self._last_total: dict[str, Any] | None = None
        self._interrupt_sent = False
        self._turn_requested = False
        self._interrupt_deadline: Deadline | None = None

    # -- writing -------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        self.transcript.add("out", message)
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise BackendError("the Codex app server closed its input") from exc

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        if method == "turn/start":
            self._turn_requested = True
        self._send({"id": request_id, "method": method, "params": params})
        while request_id not in self._responses:
            self._pump_one()
        response = self._responses.pop(request_id)
        if "error" in response:
            message = _dig(response, "error", "message") or "unknown error"
            raise BackendError(f"Codex refused {method}: {message}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    # -- reading -------------------------------------------------------------

    def wait_for_turn(self) -> dict[str, Any]:
        while self._completed_turn is None:
            self._pump_one()
        return self._completed_turn

    def _check_stop(self) -> None:
        if self.deadline.passed:
            raise StepTimedOut("the Codex step ran past its time limit")
        if self._interrupt_sent:
            if self._interrupt_deadline is not None and self._interrupt_deadline.passed:
                raise StepInterrupted("the Codex step was stopped")
            return
        if self._turn_requested and self.turn_id is None:
            # The turn id is on its way; stop with turn/interrupt once it arrives.
            return
        if self.should_stop is not None and self.should_stop():
            if self.thread_id and self.turn_id:
                self._interrupt_sent = True
                self._interrupt_deadline = Deadline(self.interrupt_grace_seconds)
                self.notify_interrupt()
            else:
                raise StepInterrupted("the Codex step was stopped")

    def notify_interrupt(self) -> None:
        self._next_id += 1
        self._send(
            {
                "id": self._next_id,
                "method": "turn/interrupt",
                "params": {"threadId": self.thread_id, "turnId": self.turn_id},
            }
        )

    def _pump_one(self) -> None:
        self._check_stop()
        wait = self.poll_seconds
        remaining = self.deadline.remaining()
        if remaining is not None:
            wait = min(wait, max(remaining, 0.0))
        line = self.reader.get(wait)
        if line is None:
            return
        if line is LineReader.EOF:
            if self._interrupt_sent:
                raise StepInterrupted("the Codex step was stopped")
            raise BackendError("the Codex app server exited before the turn finished")
        text = str(line).strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except ValueError:
            self.transcript.add("in-text", text)
            return
        self.transcript.add("in", message)
        if isinstance(message, dict):
            self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            if isinstance(message.get("id"), int):
                self._responses[message["id"]] = message
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" in message:
            self._answer(message["id"], method)
            return
        if method == "turn/started":
            turn_id = _dig(params, "turn", "id")
            if isinstance(turn_id, str) and self.turn_id is None:
                self.turn_id = turn_id
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and self._is_our_turn(params):
                self.final_message = item.get("text")
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            if isinstance(usage.get("total"), dict):
                if self._first_total is None:
                    self._first_total = usage["total"]
                    self._first_last = usage.get("last") or {}
                self._last_total = usage["total"]
        elif method == "error":
            if not params.get("willRetry"):
                self.last_error = _dig(params, "error", "message")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if self.turn_id is None or turn.get("id") == self.turn_id:
                self._completed_turn = turn

    def _is_our_turn(self, params: dict[str, Any]) -> bool:
        return self.turn_id is None or params.get("turnId") in (None, self.turn_id)

    def _answer(self, request_id: Any, method: str) -> None:
        answer = SERVER_REQUEST_ANSWERS.get(method)
        if answer is not None:
            self._send({"id": request_id, "result": answer})
        else:
            self._send(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": f"{method} is not supported by OpenDot"},
                }
            )

    def usage(self) -> dict[str, Any]:
        usage: dict[str, Any] = {"turns": 1}
        if self._last_total is None or self._first_total is None:
            return usage
        for key, name in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens")):
            before = int(self._first_total.get(key, 0)) - int(self._first_last.get(key, 0))
            usage[name] = max(int(self._last_total.get(key, 0)) - before, 0)
        return usage

    def stderr_tail(self) -> str:
        if self.stderr_reader is None:
            return ""
        while True:
            line = self.stderr_reader.get(0)
            if line is None or line is LineReader.EOF:
                break
            self._stderr.append(str(line))
        return "".join(self._stderr)[-2000:].strip()


def _close_quietly(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _same_account(original: bytes, current: bytes) -> bool:
    """True when the file the step left behind is a token refresh of the same login.

    Code in the container can write this file, so a changed file is copied back to
    the host only when it keeps the same keys, the same auth_mode and API key, and
    the same non-empty tokens.account_id. Anything else is dropped."""
    try:
        before = json.loads(original)
        after = json.loads(current)
    except ValueError:
        return False
    if not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after):
        return False
    if before.get("auth_mode") != after.get("auth_mode"):
        return False
    if before.get("OPENAI_API_KEY") != after.get("OPENAI_API_KEY"):
        return False
    tokens_before = before.get("tokens")
    tokens_after = after.get("tokens")
    if not isinstance(tokens_before, dict) or not isinstance(tokens_after, dict):
        return False
    account = tokens_before.get("account_id")
    return isinstance(account, str) and bool(account) and tokens_after.get("account_id") == account


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
