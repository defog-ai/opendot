"""What the worker image must contain, and the commands that build and check it.

The image is built from an empty context: the Dockerfile text is sent on stdin
and has no COPY from the build folder, so nothing from the host (a .env file, a
login folder) can end up in a layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from opendot.sandbox import CONTAINER_CLI_HOME, CONTAINER_GID, CONTAINER_HOME, CONTAINER_UID

# Public npm releases pinned in docker/Dockerfile. Keep the two in step.
CODEX_VERSION = "0.159.0"
CLAUDE_CODE_VERSION = "2.1.284"
UV_VERSION = "0.12.20"

# Executables every step container needs.
REQUIRED_EXECUTABLES = ("codex", "claude", "git", "node", "python3", "uv")

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


def verify_image_args(docker: str, image: str) -> list[str]:
    """Arguments for a short container that prints its user id and each tool's path."""
    checks = " ".join(REQUIRED_EXECUTABLES)
    dirs = " ".join(REQUIRED_DIRECTORIES)
    script = (
        'echo "uid=$(id -u)"; '
        f'for tool in {checks}; do echo "tool $tool $(command -v $tool || echo missing)"; done; '
        f'for dir in {dirs}; do if [ -d "$dir" ]; then echo "dir $dir ok"; '
        'else echo "dir $dir missing"; fi; done'
    )
    return [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--read-only",
        "--user",
        f"{CONTAINER_UID}:{CONTAINER_GID}",
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


def check_verify_output(returncode: int, stdout: str) -> ImageCheck:
    """Read the output of the verify_image_args command and list what is wrong."""
    problems: list[str] = []
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
    return ImageCheck(ok=not problems, problems=problems)
