"""Help text for the `opendot` command.

Each command's one-line summary lives in COMMANDS, so `opendot --help` and the
per-command help stay the same.
"""

from __future__ import annotations

DESCRIPTION = (
    "OpenDot: a self-hosted assistant that takes ongoing work, runs each model step "
    "in a locked-down container, and asks before it acts."
)

COMMANDS: dict[str, str] = {
    "init": "write a config file and create the state folder",
    "migrate": "create or update the SQLite database",
    "login": "save a login so you need no line in your shell profile (Claude Code)",
    "doctor": "check the config, the logins, Docker and the image",
    "build-image": "build the worker image from the packaged Dockerfile",
    "verify-image": "check that the worker image has every tool and runs as non-root",
    "task": "add a task from the command line",
    "ingest": "read new messages from every enabled channel once",
    "run-once": "move the oldest queued task forward by one step",
    "tick": "one full pass: schedules, wake-ups, channels, tasks and delivery",
    "status": "show task counts and the latest tasks with their answers",
    "queue": "show unfinished tasks and pending approvals",
    "show": "show everything recorded for one task",
    "approve": "grant an approval request",
    "deny": "deny an approval request",
    "retry": "queue a failed, stopped or skipped task again",
    "skip": "mark an unfinished task as skipped",
    "stop": "stop a task now",
    "notes": "list, add, edit or remove saved notes",
    "schedules": "list, add, pause, resume or end schedules",
    "rules": "list, add, approve or remove permission rules",
    "install-cron": "add (or remove) a crontab entry that runs `opendot tick`",
    "github": "repositories, task copies and what the host published on GitHub",
    "browser": "check the built-in browser",
    "connectors": "MCP connectors reached through the host gateway",
}

GETTING_STARTED = """\
Getting started without any model login (scripted fake backend):
  opendot --config ./opendot.toml init --demo --state-root ./state
  opendot --config ./opendot.toml task "Say hello"
  opendot --config ./opendot.toml run-once
  opendot --config ./opendot.toml status

With real backends:
  opendot init                 # Codex worker, Claude Code reviewer
  opendot build-image
  codex login                  # and/or: opendot login claude
  opendot doctor
  opendot task "..."
  opendot tick                 # or: opendot install-cron

Optional features (each is off until the config turns it on):
  [[repositories]] + OPENDOT_GITHUB_TOKEN   pull requests; see `opendot github`
  [browser] enabled = true                  headless Chromium; `opendot browser check`
  opendot init --with-factiq                the FactIQ connector; see `opendot connectors`

The config file is found in this order: --config, OPENDOT_CONFIG,
~/.config/opendot/opendot.toml, then built-in defaults."""


def summary(command: str) -> str:
    return COMMANDS[command]
