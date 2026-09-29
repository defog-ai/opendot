"""What the worker image must contain, and the commands that build and check it.

The image is built from an empty context: the Dockerfile text is sent on stdin
and has no COPY from the build folder, so nothing from the host (a .env file, a
login folder) can end up in a layer.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from opendot.sandbox import CONTAINER_CLI_HOME, CONTAINER_GID, CONTAINER_HOME, CONTAINER_UID

# Public npm releases pinned in docker/Dockerfile. Keep the two in step.
CODEX_VERSION = "0.159.0"
CLAUDE_CODE_VERSION = "2.1.284"
PLAYWRIGHT_MCP_VERSION = "0.0.83"
UV_VERSION = "0.12.20"

# The browser: the Playwright MCP server command and the folder that holds the
# Chromium build it expects (PLAYWRIGHT_BROWSERS_PATH in the image).
PLAYWRIGHT_MCP_COMMAND = "playwright-mcp"
PLAYWRIGHT_BROWSERS_PATH = "/opt/ms-playwright"

# Every tool that playwright-mcp 0.0.83 lists by default (checked with a live
# tools/list call). browser.tools must be a subset of these.
PLAYWRIGHT_MCP_TOOLS = frozenset(
    {
        "browser_click",
        "browser_close",
        "browser_console_messages",
        "browser_drag",
        "browser_drop",
        "browser_emulate_media",
        "browser_evaluate",
        "browser_file_upload",
        "browser_fill_form",
        "browser_find",
        "browser_handle_dialog",
        "browser_hover",
        "browser_navigate",
        "browser_navigate_back",
        "browser_network_request",
        "browser_network_requests",
        "browser_press_key",
        "browser_resize",
        "browser_run_code_unsafe",
        "browser_select_option",
        "browser_snapshot",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_type",
        "browser_wait_for",
    }
)

# Executables every step container needs.
REQUIRED_EXECUTABLES = ("codex", "claude", "git", "node", "python3", "uv", PLAYWRIGHT_MCP_COMMAND)

# The page the image check opens. It needs no network, so the check runs with
# --network none.
BROWSER_CHECK_TITLE = "opendot-browser-check"
BROWSER_CHECK_URL = f"data:text/html,<title>{BROWSER_CHECK_TITLE}</title><p>ok</p>"

# Folders that bind mounts land on. They must exist in the image because the
# root file system is read-only at run time.
REQUIRED_DIRECTORIES = ("/work", CONTAINER_CLI_HOME, CONTAINER_HOME)


def dockerfile_path() -> Path:
    """The Dockerfile shipped with the package, or the one in a source checkout."""
    try:
        packaged = resources.files("opendot").joinpath("Dockerfile")
        if packaged.is_file():
            return Path(str(packaged))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    checkout = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile"
    if checkout.is_file():
        return checkout
    raise FileNotFoundError("the worker Dockerfile is not installed with this copy of opendot")


def build_image_args(docker: str, image: str) -> list[str]:
    """Arguments for `docker build` with the Dockerfile on stdin and no build context.

    Pass dockerfile_path().read_text() as the command's input.
    """
    return [docker, "build", "--pull", "--tag", image, "-"]


def playwright_mcp_args(
    *, viewport: str, allowed_origins: Sequence[str], output_dir: str
) -> list[str]:
    """The playwright-mcp command line for a step: headless Chromium with its
    profile in memory, no page-registered tools (WebMCP), no service workers.
    Screenshots and snapshots taken without a file name go to output_dir.

    allowed_origins is passed to Playwright as a convenience. It is not a
    security boundary (it does not cover redirects); the container network is.
    """
    args = [
        PLAYWRIGHT_MCP_COMMAND,
        "--headless",
        "--isolated",
        "--browser",
        "chromium",
        "--no-webmcp",
        "--block-service-workers",
        "--viewport-size",
        viewport,
        "--output-dir",
        output_dir,
    ]
    origins = [origin.strip() for origin in allowed_origins if origin.strip()]
    if origins:
        args += ["--allowed-origins", ";".join(origins)]
    return args


# A small MCP client, run with python3 inside the container. It starts the
# server command given after the URL, opens the URL, takes a screenshot and
# prints one line: "browser ok <page title>" or "browser failed <reason>".
BROWSER_SMOKE_SCRIPT = r"""
import json, signal, subprocess, sys

def main():
    url, command = sys.argv[1], sys.argv[2:]
    signal.alarm(120)
    proc = None
    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
    def call(number, method, params):
        send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("the MCP server closed its output")
            message = json.loads(line)
            if message.get("id") == number:
                if "error" in message:
                    raise RuntimeError(str(message["error"])[:300])
                return message["result"]
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "opendot-check", "version": "1"}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names = {tool["name"] for tool in call(2, "tools/list", {})["tools"]}
        for needed in ("browser_navigate", "browser_take_screenshot"):
            if needed not in names:
                raise RuntimeError(needed + " is not offered")
        result = call(3, "tools/call", {"name": "browser_navigate", "arguments": {"url": url}})
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(text.strip()[:300])
        title = ""
        for line in text.splitlines():
            if line.strip().startswith("- Page Title:"):
                title = line.split(":", 1)[1].strip()
        shot = call(4, "tools/call", {"name": "browser_take_screenshot", "arguments": {}})
        if shot.get("isError") or not any(c.get("type") == "image" for c in shot["content"]):
            raise RuntimeError("the screenshot returned no image")
        print("browser ok " + (title or "(no title)"))
    except Exception as exc:
        print("browser failed " + " ".join(str(exc).split())[:400])
    finally:
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()

main()
"""


def browser_smoke_command(url: str, server: Sequence[str]) -> str:
    """A shell command that runs BROWSER_SMOKE_SCRIPT against url with server."""
    quoted = " ".join(shlex.quote(part) for part in (url, *server))
    return f"python3 - {quoted} <<'OPENDOT_PY'\n{BROWSER_SMOKE_SCRIPT.strip()}\nOPENDOT_PY\n"


def verify_image_args(docker: str, image: str) -> list[str]:
    """Arguments for a short container that prints its user id, each tool's path,
    every setuid or setgid file, and the result of a headless browser session on
    a page that needs no network."""
    checks = " ".join(REQUIRED_EXECUTABLES)
    dirs = " ".join(REQUIRED_DIRECTORIES)
    server = playwright_mcp_args(
        viewport="800x600", allowed_origins=(), output_dir="/tmp/opendot-browser-check"
    )
    script = (
        'echo "uid=$(id -u)"; '
        f'for tool in {checks}; do echo "tool $tool $(command -v $tool || echo missing)"; done; '
        f'for dir in {dirs}; do if [ -d "$dir" ]; then echo "dir $dir ok"; '
        'else echo "dir $dir missing"; fi; done; '
        "find / -xdev -perm /6000 -type f 2>/dev/null | sed 's/^/setuid /'; "
        + browser_smoke_command(BROWSER_CHECK_URL, server)
    )
    uid, gid = CONTAINER_UID, CONTAINER_GID
    return [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--read-only",
        "--shm-size",
        "256m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--tmpfs",
        f"{CONTAINER_HOME}:rw,nosuid,nodev,size=256m,uid={uid},gid={gid},mode=0700",
        "--env",
        f"HOME={CONTAINER_HOME}",
        "--user",
        f"{uid}:{gid}",
        "--entrypoint",
        "sh",
        image,
        "-c",
        script,
    ]


@dataclass(frozen=True)
class ImageCheck:
    ok: bool
    problems: list[str]


def check_verify_output(
    returncode: int, stdout: str, *, require_browser: bool = False
) -> ImageCheck:
    """Read the output of the verify_image_args command and list what is wrong.

    A failed browser session and any setuid or setgid file are always problems.
    A missing browser line is a problem only with require_browser, so output
    from an older check script still reads.
    """
    problems: list[str] = []
    browser_seen = False
    if returncode != 0:
        problems.append(f"the check container exited with {returncode}")
    seen_tools: set[str] = set()
    seen_dirs: set[str] = set()
    uid: str | None = None
    for line in stdout.splitlines():
        parts = line.split()
        if line.startswith("uid="):
            uid = line.removeprefix("uid=").strip()
        elif len(parts) == 3 and parts[0] == "tool":
            seen_tools.add(parts[1])
            if parts[2] == "missing":
                problems.append(f"{parts[1]} is not installed in the image")
        elif len(parts) == 3 and parts[0] == "dir":
            seen_dirs.add(parts[1])
            if parts[2] == "missing":
                problems.append(f"the folder {parts[1]} is missing from the image")
        elif line.startswith("setuid "):
            problems.append(f"{line.removeprefix('setuid ').strip()} has a setuid or setgid bit")
        elif line.startswith("browser ok"):
            browser_seen = True
        elif line.startswith("browser failed"):
            browser_seen = True
            reason = line.removeprefix("browser failed").strip()
            problems.append(f"the headless browser check failed: {reason}")
    if uid is None:
        problems.append("the check container did not report its user id")
    elif uid == "0":
        problems.append("the image runs as root")
    for tool in REQUIRED_EXECUTABLES:
        if tool not in seen_tools:
            problems.append(f"{tool} was not checked")
    for directory in REQUIRED_DIRECTORIES:
        if directory not in seen_dirs:
            problems.append(f"the folder {directory} was not checked")
    if require_browser and not browser_seen:
        problems.append("the headless browser check did not report a result")
    return ImageCheck(ok=not problems, problems=problems)
