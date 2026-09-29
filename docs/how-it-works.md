# How OpenDot works

This page explains how OpenDot decides what a model may do, and lists what is
not built yet. The [README](../README.md) has the short version.
[features.md](features.md) explains pull requests, the browser and connectors.

## Configuration

`opendot init` writes `~/.config/opendot/opendot.toml` with mode 0600. Every
key and its default is listed in [opendot.example.toml](../opendot.example.toml).
You can also set any key with an environment variable named
`OPENDOT_<SECTION>_<KEY>`, for example `OPENDOT_CORE_STATE_ROOT`.

## The trust boundary

The model never acts directly. It can only propose actions. The OpenDot host is
a Python process on your machine, and it holds everything the model does not
get: the SQLite database, the Slack token, the GitHub token, the connector
keys, your rules and the approval records.

```text
  you (CLI or Slack)
        |
        v
+----------------------------- OpenDot host (your machine) ------------------------------+
|  SQLite state   rules   approvals   notes   schedules   Slack token   outbox           |
|                                                                                        |
|  1. work step  ------------------------------> [ step container: the worker model CLI ] |
|        <------ JSON: reply, status, proposed actions                                    |
|  2. action registry: unknown kinds are refused; each action is rebuilt from the task   |
|  3. rule engine: allow / preapproved / ask / hand_off, raised to fixed floors          |
|  4. review step ----------------------------> [ step container: the other vendor ]      |
|        <------ approve or deny for each action (no answer counts as deny)               |
|  5. approval: "ask" parks the task until you approve or deny it                        |
|  6. outbox: the host posts the reply or runs the action                                 |
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
- **Rules.** Each action gets one of four levels. `allow` runs after review.
  `preapproved` runs only when a stored approval covers it. `ask` stops the task
  until you approve or deny. `hand_off` never runs; you get the prepared material
  instead. Fixed floors cannot be lowered by any rule: new schedules, rule
  changes, pushes, pull requests and connector write tools always ask; issues
  and issue comments can be lowered to `preapproved` but no further;
  credentials, payments, purchases and access changes are always handed off.
  Only the operator on the local command line can add or approve rules.
- **Reviewer.** A second model, by default from a different vendor, sees the
  request, the proposed actions and the evidence, and gives a verdict for each
  action. A failed, missing or unreadable review counts as a denial. A task stops
  after 3 denials in a row or 10 denials in the last 50 reviews.
- **Approvals.** An approval belongs to one task. A follow-up, a retry or a
  scheduled run starts with none. For an action that posts or sends something,
  the approval also covers one exact payload: the host stores a digest of it, and
  the approval does not apply if the payload changes. Only the requester, on the
  channel the task came from, and the operator on the local command line can
  decide an approval.
- **Budgets.** Each task has limits on active time, steps, turns and tokens.
  There is no money limit, because subscription logins do not report a cost.

Text from requesters, channels, web pages and files is marked as untrusted in
every prompt. The reviewer is told to judge actions against the requester's own
request, not against instructions found in that text.

## Not built yet

- Slack Socket Mode or the Events API. OpenDot polls Slack instead.
- Gmail, or any email channel or trigger.
- Proactive research when idle.
- Parallel runs: one worker runs one task step at a time.
- Rules drafted by the agent.
- A private sign-in form. The browser has no logins, and credential actions
  are always handed off to you.
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
- **An approved action runs on the next pass.** `opendot approve N` queues the
  task again. The push, pull request or connector call happens on the next
  `tick` or `run-once`, not at the moment you approve.
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
- **Codex review steps can still run shell commands.** The work folder is
  read-only in review steps and no host variables are passed, but the network
  follows the config. Claude Code and opencode review steps get no tools.
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
