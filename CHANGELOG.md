# Changelog

All notable changes to OpenDot are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

The host now runs every action the model proposes, without approvals, rules or
a reviewer. This is a breaking change.

### Removed

- Approvals. `opendot approve N`, `opendot deny N` and the "approve N" and
  "deny N" replies in Slack are gone. A Slack reply of "approve N" or "deny N"
  now gets a short message that explains the change.
- Rules and their levels (`allow`, `preapproved`, `ask`, `hand_off`), the fixed
  floors, and the `opendot rules` command. Every known action runs as soon as
  the model proposes it.
- The reviewer model and the review step. `opendot init` no longer takes
  `--reviewer`, and the default setup uses Codex only.

### Changed

- A config file that still has `[reviewer]`, `[backend.reviewer]` or `[rules]`
  loads without an error. OpenDot ignores those sections, and `opendot doctor`
  lists them so you can delete them.
- The database moves to version 3 the first time a new `opendot` command
  opens it. An action that was waiting for an approval is marked as not run,
  and its task is queued again so the model can propose the action again. The
  old approval, rule and review tables stay in the file but are not read.
- A stopped task runs none of the actions that are still left in its step.

### Added

- `opendot login claude` runs `claude setup-token`, asks for the token and saves
  it in `backend.claude_code.token_file` (default
  `~/.config/opendot/claude-token`, mode 600). The Claude Code backend reads the
  file when `backend.claude_code.token_env` is not set, so the token needs no
  line in a shell profile and cron runs find it. `opendot doctor` and
  `opendot install-cron` point to the command when no login is found.

## [0.2.0] - 2026-09-30

Pull requests, a built-in browser and MCP connectors, each off by default. The
host still holds every credential that can act outward, and every outward
action still goes through rules, the reviewer and an approval tied to its
payload.

### Added

- Pull requests. `[github]` and `[[repositories]]` give each task its own copy
  of a repository. The model edits files; the host commits, runs the checks in
  a separate container and proposes the actions `github.push_branch`,
  `github.open_pr`, `github.issue` and `github.issue_comment`. Pushes and pull
  requests always ask. The GitHub token never enters a container. A public
  repository gets a text check for private paths, emails and token-shaped text.
  The approval shows every text line of the change, whatever
  `.gitattributes` says. A diff longer than `github.max_diff_chars` is
  refused, and so is a binary file in a public repository unless
  `github.allow_binary_public = true`. The host never edits a pull request,
  issue or comment that another account opened.
- `forge = "none"` for a repository on a plain git remote. It needs no token
  and allows `github.push_branch` only. The operator states its `visibility`,
  and `github.author_email` is required.
- A built-in browser. `[browser] enabled = true` gives work steps a headless
  Chromium through the Playwright MCP server inside the step container, with a
  fixed list of allowed tools. Screenshots and snapshots are saved in the
  step's run folder.
- MCP connectors. `[[mcp_servers]]` entries are reached through a gateway on
  the host that keeps the keys. Only tools marked `read` are callable from a
  step; tools marked `write` become `mcp.<server>.<tool>` actions that ask.
  Calls are logged, results are capped and slow calls are stopped. Bearer keys
  from host variables and OAuth sign-in are supported. A connector that runs a
  program on the host needs `allow_host_command = true`. A connector address
  must use https, except for `127.0.0.1`, `localhost` and `::1`.
- An opencode backend (`kind = "opencode"`), so a step can use any provider
  that opencode supports, such as OpenRouter or an opencode plan. The model
  name is `provider/model`. Only the login for that provider is copied into
  the container, and it is deleted when the step ends. Work steps get the
  browser and connectors like the other backends; review and reflect steps
  get no tools.
- FactIQ as a built-in connector preset (`[factiq]`, `opendot init
  --with-factiq`) for `https://api.factiq.com/mcp`, with the skill files of the
  public factiq-plugin repository mounted read-only.
- Commands: `github repos|fetch|copies|publications|prune|reconcile`,
  `browser check`, and `connectors
  list|login|logout|test|calls|fetch-instructions`.
- `opendot doctor` checks git, the GitHub token and remotes, the browser tools
  and shared memory, each connector's login, and the FactIQ plugin files.
- `opendot doctor` detects Docker from a snap package. It reports an error
  while `sandbox.no_new_privileges` is true or a mounted folder is under
  `/tmp`, and says what to change.
- `opendot verify-image` also opens a local page in the step image's browser.
- The work prompt lists the tools and folders each step gets, and the review
  prompt tells the reviewer which fields the host wrote.

### Changed

- The README is rewritten for new users: what OpenDot can do, which
  subscriptions it can use, how to pick models, and an animation of two tasks.
  The full details moved to `docs/how-it-works.md` and `docs/features.md`.
- The work prompt now names `/work` as the writable working folder. It named a
  folder that does not exist before.
- `sandbox.env_allowlist` may not name the GitHub token variable or a
  connector key variable.

Configs from 0.1.0 work without changes. A 0.1.0 database is updated in place
the first time any command opens it: the new tables are added and every row
is kept.

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

See "Not built yet" and "Known limits" in docs/how-it-works.md, and
SECURITY.md.
