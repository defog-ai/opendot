# OpenDot

OpenDot is a self-hosted assistant that you run on your own machine. You give it
work from the command line or from Slack. A coding-agent CLI (OpenAI Codex or
Claude Code) does each model step inside a locked-down Docker container. The
OpenDot host on your machine decides which proposed actions actually happen: it
checks each one against your rules, has a second model from a different vendor
review it, and asks you when a rule says so. OpenDot can wait and come back to a
task later, run saved schedules, and keep private notes about each person it
works for.

OpenDot is inspired by OpenAI's dots, which OpenAI
[announced](https://openai.com/index/introducing-dots/) on 2026-09-29.
**OpenDot is not affiliated with or endorsed by OpenAI.** It is an independent
open-source project under the Apache 2.0 license.

Version 0.1 is an early release. Read [SECURITY.md](SECURITY.md) before you give
it real work: the step containers have network access by default, and they hold
the model login they need.

## Quickstart

You need Linux (macOS is untested), Python 3.11 or newer, [uv](https://docs.astral.sh/uv/)
and, for real model steps, Docker from docker.com (see "Known limits").

### 1. Try it with no model and no Docker

The `--demo` flag sets up a scripted fake backend. It shows the full path of a
task (work step, review, reply) without a login or a container.

```sh
uv tool install git+https://github.com/defog-ai/opendot
mkdir opendot-demo && cd opendot-demo
opendot --config ./opendot.toml init --demo --state-root ./state
opendot --config ./opendot.toml task "What can you do?"
opendot --config ./opendot.toml run-once
opendot --config ./opendot.toml status
```

The last command prints the task and its answer:

```text
Tasks: done 1
#1 done              operator: What can you do?
    answer: Hello. This answer comes from the scripted fake backend, so no model ran. ...
```

The demo script answers one task only. The scripted review approves action 1
by number, so a second task's reply gets no verdict, counts as denied, and is not
sent. Run `opendot --config ./opendot.toml show 1`
to see every step, action and event that was recorded.

### 2. Run it with real models

```sh
opendot init                   # Codex does the work, Claude Code reviews
opendot build-image            # builds opendot-worker:latest, takes a few minutes
opendot verify-image           # checks the tools and the non-root user
codex login                    # writes ~/.codex/auth.json
claude setup-token             # then: export CLAUDE_CODE_OAUTH_TOKEN=...
opendot doctor                 # reports what is still missing
opendot task "Summarise the three main points of RFC 9110 section 9"
opendot tick                   # one full pass; repeat, or:
opendot install-cron           # runs `opendot tick` every minute from crontab
opendot status
```

`opendot init` writes `~/.config/opendot/opendot.toml` with mode 0600. Every
key and its default is listed in [opendot.example.toml](opendot.example.toml).
You can also set any key with an environment variable named
`OPENDOT_<SECTION>_<KEY>`, for example `OPENDOT_CORE_STATE_ROOT`.

To use one vendor only, pass `--worker` and `--reviewer` to `opendot init`, for
example `--worker claude_code --reviewer claude_code`. `opendot doctor` then
warns you, because a reviewer from a different vendor gives a more independent
check.

### 3. Optional: Slack

Create a Slack app from [slack-app-manifest.yml](slack-app-manifest.yml), install
it in your workspace, and invite the bot to the channels it should read. Then set
these keys:

```toml
[channels.slack]
enabled = true
bot_token_env = "OPENDOT_SLACK_BOT_TOKEN"   # the variable that holds the xoxb- token
channels = ["C0EXAMPLE1"]                  # channel ids to read
allowed_users = ["U0EXAMPLE1"]             # member ids that may give work
```

An empty `allowed_users` list lets nobody give work. OpenDot reads Slack by
polling on each `tick`, so replies arrive on the next pass, not at once.

## Commands

| Command | What it does |
| --- | --- |
| `init` | Write a config file and create the state folder. |
| `migrate` | Create or update the SQLite database. |
| `doctor` | Check the config, the logins, Docker and the image. |
| `build-image`, `verify-image` | Build the step image from the packaged Dockerfile, then check it. |
| `task "..."` | Add a task from the command line. |
| `ingest` | Read new messages from every enabled channel once. |
| `run-once` | Move the oldest queued task forward by one step. |
| `tick` | One full pass: expire approvals, wake waiting tasks, start due schedules, read channels, run tasks, deliver replies. |
| `status`, `queue`, `show N` | Look at task counts and answers, unfinished work and approvals, or one task in full. |
| `approve N`, `deny N` | Decide an approval request. |
| `retry N`, `skip N`, `stop N` | Queue a finished task again, skip an unfinished one, or stop one now. |
| `notes`, `schedules`, `rules` | List and change saved notes, schedules and permission rules. |
| `install-cron` | Add or remove the crontab line that runs `opendot tick`. |

Run `opendot COMMAND --help` for the options of each command.

## The trust boundary

The model never acts directly. It can only propose actions. The OpenDot host is
a Python process on your machine, and it holds everything the model does not
get: the SQLite database, the Slack token, your rules and the approval records.

```text
  you (CLI or Slack)
        |
        v
+----------------------------- OpenDot host (your machine) ------------------------------+
|  SQLite state   rules   approvals   notes   schedules   Slack token   outbox           |
|                                                                                        |
|  1. work step  ------------------------------> [ step container: Codex or Claude Code ] |
|        <------ JSON: reply, status, proposed actions                                    |
|  2. action registry: unknown kinds are refused; each action is rebuilt from the task   |
|  3. rule engine: allow / preapproved / ask / hand_off, raised to fixed floors          |
|  4. review step ----------------------------> [ step container: the other vendor ]      |
|        <------ approve or deny for each action (no answer counts as deny)               |
|  5. approval: "ask" parks the task until you approve or deny it                        |
|  6. outbox: the host posts the reply or runs the action                                 |
+----------------------------------------------------------------------------------------+
```

What each part does:

- **Step container.** Each model step runs in a new `docker run --rm` container.
  It has no Linux capabilities, a read-only root file system, CPU, memory and
  process limits, and runs as your user id, not root. It never gets the Docker
  socket, the state folder or the Slack token. It gets one work folder, the
  session files of its own CLI, and the one login that CLI needs.
- **Action registry.** OpenDot v0.1 has four action kinds: `reply.post` (answer
  in the requester's thread), `notify.post` (send a schedule's result to the
  place the schedule names), `note.write` (save a note about the requester) and
  `schedule.create` (save a new schedule). The host builds the target of each
  action from the task itself, not from the model's text, so a reply can only go
  back to the thread the request came from.
- **Rules.** Each action gets one of four levels. `allow` runs after review.
  `preapproved` runs only when a stored approval covers it. `ask` stops the task
  until you approve or deny. `hand_off` never runs; you get the prepared material
  instead. Fixed floors cannot be lowered by any rule: new schedules and rule
  changes always ask; credentials, payments, purchases and access changes are
  always handed off. Only the operator on the local command line can add or
  approve rules.
- **Reviewer.** A second model, by default from a different vendor, sees the
  request, the proposed actions and the evidence, and gives a verdict for each
  action. A failed, missing or unreadable review counts as a denial. A task stops
  after 3 denials in a row or 10 denials in the last 50 reviews. These cutoffs
  are the ones that [Codex auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review)
  documents.
- **Approvals.** An approval belongs to one task. A follow-up, a retry or a
  scheduled run starts with none. For an action that posts or sends something,
  the approval also covers one exact payload: the host stores a digest of it, and
  the approval does not apply if the payload changes. Only the requester, on the
  channel the task came from, and the operator on the local command line can
  decide an approval.
- **Budgets.** Each task has limits on active time, steps, turns and tokens.
  There is no money limit, because subscription logins do not report a cost.

Text from requesters, channels and files is marked as untrusted in every prompt.
The reviewer is told to judge actions against the requester's own request, not
against instructions found in that text.

## Compared with OpenAI's dots

This table compares OpenDot v0.1 with features OpenAI has described in public.
Each row links to the OpenAI page it is based on. "Similar" means OpenDot has a
feature of the same kind, not that it works the same way or as well.

| Feature described by OpenAI | Source | OpenDot v0.1 |
| --- | --- | --- |
| Always-on agent that keeps working on ongoing work between conversations | [Introducing dots](https://openai.com/index/introducing-dots/) | **Partial.** One worker on your machine, started by cron or `tick --loop`. Tasks keep their state between passes. |
| Talk to it in Slack | [Getting started with your dot](https://help.openai.com/en/articles/20001530-getting-started-with-your-dot) | **Similar.** Slack (by polling) and the local command line. |
| Pauses and wakes up to continue work | [Tasks and memory](https://learn.chatgpt.com/codex/dots/tasks-and-memory.md) | **Similar.** A step can end with "wait until"; `tick` wakes the task at that time. |
| Saved schedules with time zone, end date, notify rule and destination | [Tasks and memory](https://learn.chatgpt.com/codex/dots/tasks-and-memory.md) | **Similar.** Cron cadence, IANA time zone, optional end, notify `always` or `changed`, fixed destination. |
| Work started by events from connected services | [Tasks and memory](https://learn.chatgpt.com/codex/dots/tasks-and-memory.md), [Automations](https://learn.chatgpt.com/docs/automations.md) | **Partial.** Only new Slack messages from allowed users. |
| Four rule levels, plus actions that always need confirmation or a hand-off | [Controls](https://learn.chatgpt.com/codex/dots/controls.md), [Safety blog](https://openai.com/index/how-we-build-safety-security-and-privacy-into-dots/) | **Similar.** The four levels are modelled on the ones OpenAI describes, with fixed floors. |
| A separate reviewer checks each action; the turn stops after 3 denials in a row or 10 in the last 50 | [Auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review) | **Similar.** Same cutoffs by default; the reviewer can be a different vendor. |
| An approval stays tied to its task | [Safety blog](https://openai.com/index/how-we-build-safety-security-and-privacy-into-dots/) | **Similar.** Tied to the task and to a digest of the exact payload. |
| The agent drafts rule changes and the user approves each one | [Controls](https://learn.chatgpt.com/codex/dots/controls.md) | **Not in v0.1.** Only the operator adds rules. |
| Private notes about preferences and ongoing work | [Tasks and memory](https://learn.chatgpt.com/codex/dots/tasks-and-memory.md) | **Similar.** Notes per requester, which the operator can list, edit and delete. |
| Background agents that run in parallel | [Tasks and memory](https://learn.chatgpt.com/codex/dots/tasks-and-memory.md) | **Not in v0.1.** One task step runs at a time. |
| Its own cloud computer with a browser | [dots in Codex](https://learn.chatgpt.com/codex/dots), [Safety blog](https://openai.com/index/how-we-build-safety-security-and-privacy-into-dots/) | **Not in v0.1.** A container with a shell and a work folder, no browser. |
| Apps and MCP servers through plugins | [Plugins](https://learn.chatgpt.com/docs/plugins.md) | **Not in v0.1.** |
| Proactive research with read-only tools when idle | [Safety blog](https://openai.com/index/how-we-build-safety-security-and-privacy-into-dots/) | **Not in v0.1.** |
| A private sign-in form, so the model never sees credentials | [Credentials FAQ](https://help.openai.com/articles/20001529), [Safety blog](https://openai.com/index/how-we-build-safety-security-and-privacy-into-dots/) | **Not in v0.1.** Credential actions are always handed off to you. |

## Not in v0.1

These are not built yet:

- Coding tasks that end in a pull request.
- Slack Socket Mode or the Events API. OpenDot polls Slack instead.
- A browser inside the step container.
- MCP servers and app connectors.
- Gmail, or any email channel or trigger.
- Proactive research when idle.
- Parallel runs: one worker runs one task step at a time.
- Rules drafted by the agent.
- The `anthropic_api` backend. The file exists, but `opendot doctor` reports it
  as not ready.

## Known limits

- **Docker from a snap package needs two settings.** Snap Docker refuses
  containers started with `no-new-privileges`, so set
  `sandbox.no_new_privileges = false`. It also cannot see mount sources under
  `/tmp`, so keep `core.state_root` and every `sandbox.readonly_mounts` folder
  outside `/tmp`. The containers still drop every Linux capability, keep a
  read-only root file system and run as a non-root user. What you lose is the
  guard against setuid programs inside the image. Docker from docker.com keeps
  every setting. Rootless Docker is untested.
- **Network access is open by default.** Step containers use the Docker network
  `bridge`, so they can reach any host your machine can reach. With
  `sandbox.network = "none"` the model CLIs cannot reach their own API. See
  [SECURITY.md](SECURITY.md).
- **Claude Code has no turn limit flag,** so the per-step turn limit applies only
  to Codex. The task turn and time budgets still apply to both.
- **Codex review steps can still run shell commands.** The work folder is
  read-only in review steps and no host variables are passed, but the network
  follows the config.
- **A Codex token refresh inside the container is written back to your
  `~/.codex/auth.json`.** OpenDot copies it back only when the file still
  belongs to the same account and your own file did not change during the step.
- **A follow-up from a different person starts fresh.** When another allowed
  user mentions OpenDot in a finished thread, the new task does not see the
  earlier request or its reply, because the earlier session holds the first
  requester's notes. A follow-up from the same person continues that session.
- **Approval and hand-off messages show the proposed text in the task's
  thread.** In a shared Slack channel, other members can read a proposed reply
  before you approve it.
- **Resuming a Claude Code session in a later step is untested.**
- **Codex steering and a retry of a stalled Codex turn are not built.** A stop
  message ends the step; other messages are read at the start of the next step.
- **Slack rate limits.** Read a few channels only, run `tick` at most once a
  minute, and expect at most 20 active threads to be read on each pass.
- **Replies from the fake backend are scripted.** Use it for tests and the demo.

## Pull requests

**OpenDot can push a branch, open a pull request, open an issue and comment on
an issue. The model only edits files; the host commits, checks and publishes,
and every one of these is an action that goes through rules, review and your
approval.** It is off until you add a repository:

```toml
[github]
# The host reads the token from this variable. It never enters a container.
token_env = "OPENDOT_GITHUB_TOKEN"

[[repositories]]
name = "app"
remote = "https://github.com/example/app.git"
checks = ["uv run pytest -q"]
```

Set `OPENDOT_GITHUB_TOKEN` to a fine-grained token that can write contents,
pull requests and issues of that repository only. `opendot doctor` checks that
git is installed, the token is set and each remote is on GitHub.

How it works:

1. **Each task gets its own copy.** Before the first work step the host fetches
   a bare clone of the default branch (`repos/<name>` under the state root),
   copies it to `worktrees/task-<id>/<name>`, creates the branch
   `opendot/task-<id>` and runs the `prepare` commands in a sandbox container.
   The copy is mounted at `/opendot/repos/<name>`. Its `.git` folder is
   read-only in the container, so the model cannot commit, push or change a
   remote.
2. **The host builds the action.** When the model proposes `github.push_branch`
   or `github.open_pr`, the host commits the files (with git hooks, the host
   user's git config and submodules switched off), refuses nested `.git`
   folders, files that match `github.forbidden_files`, files larger than
   `github.max_file_kib` and symbolic links that point outside the repository,
   then runs the `checks` in a separate container on `check_network` (`none`
   by default). A failed check refuses the action. A check that changes files
   also refuses it. The action shows the commit, the tree hash, the changed
   files, the diff (first 20,000 characters) and the check output.
3. **Public repositories get a text check.** The host asks GitHub whether the
   repository is public. If GitHub does not answer, the action is refused. For
   a public repository, the action is refused when the added lines, file names,
   commit messages, title or body contain a home-folder path, an email address
   outside the example domains, text shaped like a token, a link to a coding
   session or a `github.private_markers` entry. If the repository is public and
   `public = false`, the host refuses every action for it.
4. **Publishing happens only after approval.** Push and pull request actions
   ask you every time; that is their floor. Issues and comments ask by default,
   and a rule can lower them to `preapproved`, but no further. Before it
   pushes, the host checks that the copy still holds the approved commit and
   tree, and that the repository is still public or private as it was. It
   never force-pushes and never pushes to the default branch.
5. **Retries do not publish twice.** Each pull request, issue and comment
   carries a hidden marker made from its content. A retry finds the earlier
   result through the marker, first in the database and then on GitHub. A new
   commit on a task that already has a pull request is pushed to the same
   pull request.

Commands: `opendot github repos`, `fetch`, `copies`, `publications`,
`prune` (deletes the copies of finished tasks) and `reconcile` (records pull
requests and issues that were merged or closed).

Limits:

- A check that needs the network needs `check_network` set to a Docker network
  that can reach it. The check container gets no token and no host variables.
- Files that `prepare` writes into the copy are committed unless the
  repository's `.gitignore` covers them.
- The text check matches patterns. It is a guard against mistakes, not a
  guarantee. Review the diff before you approve.
- An ssh remote uses the host user's ssh agent for fetch and push. An https
  remote uses the token.

## Browser

**Work steps can use a headless Chromium browser, which runs inside the step
container and nowhere else.** It is off by default. Turn it on in
`opendot.toml`:

```toml
[browser]
enabled = true
# The other settings and their defaults:
# viewport = "1280x800"
# shm_size = "1g"
# allowed_origins = []
```

- The step image holds Chromium and the Playwright MCP server
  (`@playwright/mcp` 0.0.83). Each work step gets one MCP server named
  `browser`. The model may call only the tools in `browser.tools`. By default
  that list leaves out the tools that run page scripts or code
  (`browser_evaluate`, `browser_run_code_unsafe`), file upload and form filling. Codex hides the other tools. Claude Code refuses them.
- Screenshots and page snapshots taken without a file name are saved in
  `/opendot/run/artifacts`. That folder is `runs/task-<id>/<step token>/artifacts`
  under the state folder, so you can open the files after the step.
- The browser starts with an empty profile in memory. It has no cookies, no
  saved logins and no host credentials.
- The browser uses the network of the step container (`sandbox.network`). That
  network is the security boundary. `browser.allowed_origins` is passed to
  Playwright, but it does not cover redirects and is not a boundary. With
  `sandbox.network = "none"` the browser cannot open web pages.
- Chromium's own sandbox is off. The container drops every Linux capability,
  and Chromium cannot build its sandbox without them. The container is the
  boundary.
- Page text is untrusted. The model reads it as data. Anything outward still
  goes through actions, rules, the reviewer and your approval.

Check the browser with `opendot verify-image`, which opens a local page with
no network, and with `opendot browser check --url https://example.com`, which
opens a real page with the sandbox settings from your config.

## Development

```sh
git clone https://github.com/defog-ai/opendot && cd opendot
uv sync
uv run pytest -q
uv run ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
