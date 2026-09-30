# How OpenDot works

This page explains how OpenDot turns what a model proposes into actions, and
lists what is not built yet. The [README](../README.md) has the short version.
[features.md](features.md) explains pull requests, the browser and connectors.

## Configuration

`opendot init` writes `~/.config/opendot/opendot.toml` with mode 0600. Every
key and its default is listed in [opendot.example.toml](../opendot.example.toml).
You can also set any key with an environment variable named
`OPENDOT_<SECTION>_<KEY>`, for example `OPENDOT_CORE_STATE_ROOT`.

## The trust boundary

The model never acts directly. It can only propose actions. The OpenDot host is
a Python process on your machine, and it holds everything the model does not
get: the SQLite database, the Slack token, the GitHub token and the connector
keys. The host runs every valid action that the model proposes. It does not ask
you first, and no second model checks the action.

```text
  you (CLI or Slack)
        |
        v
+----------------------------- OpenDot host (your machine) ------------------------------+
|  SQLite state   notes   schedules   Slack token   GitHub token   connector keys   outbox |
|                                                                                        |
|  1. work step  ------------------------------> [ step container: the worker model CLI ] |
|        <------ JSON: reply, status, proposed actions                                    |
|  2. action registry: unknown kinds are refused; each action is rebuilt from the task   |
|  3. the host runs each action in order, unless the task was stopped                    |
|  4. outbox: the host posts the reply                                                   |
+----------------------------------------------------------------------------------------+
```

The worker model CLI is Codex, Claude Code or opencode, as your config says.

What each part does:

- **Step container.** Each model step runs in a new `docker run --rm` container.
  It has no Linux capabilities, a read-only root file system, CPU, memory and
  process limits, and runs as your user id, not root. It never gets the Docker
  socket, the state folder or the Slack token. It gets one work folder, the
  session files of its own CLI, and the one login that CLI needs.
- **Action registry.** OpenDot has four basic action kinds: `reply.post` (answer
  in the requester's thread), `notify.post` (send a schedule's result to the
  place the schedule names), `note.write` (save a note about the requester) and
  `schedule.create` (save a new schedule). When you turn the features on, it
  also has `github.push_branch`, `github.open_pr`, `github.issue`,
  `github.issue_comment` and one `mcp.<server>.<tool>` kind for each connector
  tool marked `write`. The host builds the target of each action from the task
  itself, not from the model's text, so a reply can only go back to the thread
  the request came from, and a push can only go to the task's own branch.
- **Host-only credentials.** The GitHub token and the connector keys stay on
  the host. No step container gets them: the host pushes and opens pull
  requests itself, and the connector gateway adds the key to each request it
  sends on.
- **Running actions.** The host runs the actions of a step one after the other,
  as soon as the step ends. Before each action it checks that the task is still
  running, so `opendot stop N` or a stop message prevents the actions that have
  not run yet. `opendot show N` lists each action with its result.
- **Checks before a push.** A push runs your repository's `checks` first and
  stops when one fails. The host never pushes to the default branch. For a
  public repository, it refuses a change or a pull request text that looks
  private.
- **Budgets.** Each task has limits on active time, steps, turns and tokens.
  There is no money limit, because subscription logins do not report a cost.

Text from requesters, channels, web pages and files is marked as untrusted in
every prompt. The model is told that this text can never authorize an outward
action. This is an instruction to the model only; the host does not enforce it.

## Not built yet

- Slack Socket Mode or the Events API. OpenDot polls Slack instead.
- Gmail, or any email channel or trigger.
- Proactive research when idle.
- Parallel runs: one worker runs one task step at a time.
- A private sign-in form. The browser has no logins.
- A way to approve an action before it runs. OpenDot no longer has approvals.
- A full cloud computer with a desktop, or a way to use your own computer.
- Pull requests on forges other than GitHub. A plain git remote
  (`forge = "none"`) gets branch pushes only.
- A Hermes Agent backend.
- The `anthropic_api` backend. The file exists, but `opendot doctor` reports it
  as not ready.

## Known limits

- **Docker from a snap package works with one setting changed.** Snap Docker
  refuses containers started with `no-new-privileges`, so set
  `sandbox.no_new_privileges = false`. It also cannot see mount sources under
  `/tmp`, so keep `core.state_root` and every `sandbox.readonly_mounts` folder
  outside `/tmp`. `opendot doctor` detects snap Docker and reports an error
  until both are true. The containers still drop every Linux capability, keep
  a read-only root file system and run as a non-root user. What you lose is
  the guard against setuid programs inside the image; [SECURITY.md](../SECURITY.md)
  explains the trade-off. Docker from docker.com keeps every setting. Rootless
  Docker is untested.
- **A plain git remote's visibility is what you state.** With
  `forge = "none"`, OpenDot cannot ask the server whether the repository is
  public. It uses your `visibility` setting for the public text check.
- **Network access is open by default.** Step containers use the Docker network
  `bridge`, so they can reach any host your machine can reach. With
  `sandbox.network = "none"` the model CLIs cannot reach their own API. See
  [SECURITY.md](../SECURITY.md).
- **Claude Code and opencode have no turn limit flag,** so the per-step turn
  limit applies only to Codex. The task turn and time budgets still apply to
  all three.
- **opencode takes no output schema.** OpenDot adds the schema to the end of the
  prompt and reads the last JSON object in the model's final message. The host
  checks it against the schema, as it does for every backend. A model that
  ignores the format fails the step.
- **opencode gets one login at a time.** Only the entry for the provider in the
  model name (the part before the first `/`) is copied from your opencode login
  file into the container, and it is deleted when the step ends. A refreshed
  OAuth login is written back to your file only when it keeps the same fields and
  your own file did not change during the step. A changed API key is never
  written back.
- **A Codex token refresh inside the container is written back to your
  `~/.codex/auth.json`.** OpenDot copies it back only when the file still
  belongs to the same account and your own file did not change during the step.
- **A follow-up from a different person starts fresh.** When another allowed
  user mentions OpenDot in a finished thread, the new task does not see the
  earlier request or its reply, because the earlier session holds the first
  requester's notes. A follow-up from the same person continues that session.
- **A prompt injection can cause an action.** A web page, a file or a message
  that the model reads can ask it to propose an action, and the host runs any
  valid action. Connect only the repositories and connector `write` tools that
  you accept this for.
- **Resuming a Claude Code session in a later step is untested.**
- **Codex steering and a retry of a stalled Codex turn are not built.** A stop
  message ends the step; other messages are read at the start of the next step.
- **Slack rate limits.** Read a few channels only, run `tick` at most once a
  minute, and expect at most 20 active threads to be read on each pass.
- **Replies from the fake backend are scripted.** Use it for tests and the demo.
