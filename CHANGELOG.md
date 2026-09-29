# Changelog

All notable changes to OpenDot are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-30

First public release.

### Added

- The `opendot` command with 21 commands: `init`, `migrate`, `doctor`,
  `build-image`, `verify-image`, `task`, `ingest`, `run-once`, `tick`,
  `status`, `queue`, `show`, `approve`, `deny`, `retry`, `skip`, `stop`,
  `notes`, `schedules`, `rules` and `install-cron`.
- `opendot init --demo`, which runs a task end to end with a scripted fake
  backend and needs no model login or Docker.
- Backends for the Codex CLI (app-server protocol) and the Claude Code CLI
  (print mode with stream-json output). Every model step runs in a new Docker
  container with no capabilities, a read-only root file system and resource
  limits.
- A work step, a review step by a separate model, and a reflect step that
  writes notes.
- Four action kinds: `reply.post`, `notify.post`, `note.write` and
  `schedule.create`.
- Rules with four levels (`allow`, `preapproved`, `ask`, `hand_off`) and fixed
  floors that no rule can lower.
- Approvals tied to one task, and for outward actions to a digest of the exact
  payload.
- A reviewer that fails closed, and a task stop after 3 denials in a row or 10
  in the last 50 reviews.
- Tasks that wait and wake at a set time, and saved schedules with a cron
  cadence, a time zone, an optional end, and a notify rule.
- Private notes per requester.
- Channels: the local command line, and Slack by polling.
- A SQLite store with leases, an outbox with retries and an event log.
- A crontab entry for `opendot tick`, managed by `opendot install-cron`.

### Known limits

See "Not in v0.1" and "Known limits" in the README, and SECURITY.md.
