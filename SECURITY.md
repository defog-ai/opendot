# Security

OpenDot runs model-written commands on your machine inside Docker containers.
The containers limit what those commands can reach, but they do not make them
safe. In v0.1 a step container has open network access by default, and it holds
the model login it needs. Treat anything the step container can reach as
something the model can read or send.

## Reporting a problem

Email **security@defog.ai** with the steps to reproduce the problem and the
OpenDot version (`opendot --version`). Do not open a public issue for a security
problem.

## What the host protects

The OpenDot host is the `opendot` process on your machine. It keeps these items
out of every step container:

- the SQLite database, the state folder and the log files;
- the Slack bot token;
- your rules, approval records and other people's notes;
- the Docker socket;
- host environment variables, unless you name them in `sandbox.env_allowlist`
  (work steps only; review steps never get any). The allowlist may not name the
  Slack token variable or the Claude Code login variable.

The model cannot run an action by itself. It proposes actions in its JSON
output. The host refuses unknown action kinds, builds each action's target from
the task (not from the model's text), applies your rules and the fixed floors,
asks a separate reviewer, and waits for your approval when a rule says "ask".
Only then does the host post or save anything.

Each step container:

- starts fresh with `docker run --rm`;
- drops every Linux capability and, unless `sandbox.no_new_privileges` is
  false, sets `no-new-privileges`;
- has a read-only root file system, with size-limited tmpfs folders for `/tmp`
  and the home folder;
- runs as your user id (or a fixed non-root user when you run OpenDot as root);
- has CPU, memory and process limits;
- mounts only its work folder and its CLI session folder, plus any read-only
  folders you list in `sandbox.readonly_mounts`. In review steps the work folder
  is read-only.

## What the host does not protect

Read this section before you give OpenDot real work.

### Network access

The default Docker network is `bridge`. A step container can connect to any
address your machine can reach: the internet, your local network, and services
that listen on the Docker bridge address of your machine. The model CLIs need
this to reach their own API.

If you set `sandbox.network = "none"`, the container has no network, and the
Codex and Claude Code CLIs cannot reach their API, so steps fail. You can create
your own Docker network that only allows the model API (for example, through an
egress proxy) and put its name in `sandbox.network`. OpenDot v0.1 does not ship
such a network. `network = "host"` and `network = "container:<name>"` are
refused.

Because of this, a model that reads hostile text (a web page, a file, a message
from someone else in a Slack thread) could be led to send data it can see to
another server. Keep secrets and private files out of the work folder and out of
`readonly_mounts`.

### The model login is inside the container

A step needs the login of its own CLI:

- **Codex:** OpenDot copies your `~/.codex/auth.json` into the session folder
  that is mounted into the container. If Codex refreshes the token during the
  step, OpenDot copies the new file back to your home folder so your own login
  stays valid. It does this only when the new file has the same `auth_mode`,
  the same API key field and the same `tokens.account_id`, and your own file
  did not change during the step. Otherwise the new file is deleted.
- **Claude Code:** OpenDot passes the variable named in
  `backend.claude_code.token_env` (default `CLAUDE_CODE_OAUTH_TOKEN`) into the
  container.

Code that runs in the container can read that login and, with network access,
send it elsewhere. Use a login that you can revoke, and revoke it if you think a
step was misused. Prefer a token from `claude setup-token` or an API key with a
spending limit over your main account password.

### Review steps can run commands

The reviewer is a model CLI too. Codex review steps can still run shell commands
inside their container. The work folder is read-only in review steps and no host
variables are passed, but the network follows your config.

### The reviewer is a model

The reviewer is told to judge each action against the requester's own request
and to treat all supplied text as untrusted evidence. It can still be wrong or be
misled. A failed or unreadable review counts as a denial, and a task stops after
repeated denials. Your rules and approvals are the checks that do not depend on
a model: use `ask` for actions you want to see first.

### Who may give work

On Slack, only the member ids in `channels.slack.allowed_users` can create
tasks, and an empty list allows nobody. An approval can be decided only by the
task's requester, on the channel the task came from, or by the operator on the
local command line. Messages from other people in a task's thread are not given
to the model. Text that the requester pastes, and files or pages that the model
reads, can still hold hostile instructions.

A follow-up from a different allowed user in a finished thread starts a new
model session with that user's own notes. It does not continue the earlier
requester's session.

### What is posted before you approve

When an action needs your approval, or is handed off to you, OpenDot posts the
proposed payload (for example the text of a reply) in the task's thread so you
can judge it. In a shared Slack channel, the other members of the channel can
read that text before you decide. The reviewer has already approved the action
at that point, but you have not.

### Notes the model writes

A note is read by every later task for the same requester, so `note.write`
defaults to `ask`. It defaults to `allow` when the proposal cites, as its
source, a message that the requester wrote in the current task. The host checks
that the cited message belongs to this task and to this requester. It does not
check that the note text matches the message, because a note is the model's
summary of it. The reviewer still sees every note write, and a rule can set
`note.write` to `ask` for all notes.
On the command line, anyone who can run `opendot` as your user is the operator
and can change rules and approvals.

### Docker itself

A user who can run Docker commands can become root on most machines. OpenDot
needs Docker, so the user that runs OpenDot has that power. Rootless Docker is
untested.

Docker from a snap package refuses the `no-new-privileges` option. To use it, set
`sandbox.no_new_privileges = false` and keep the state folder and read-only
mounts outside `/tmp`. Without that option, a setuid program inside the image
could take the user id of its owner (usually root) inside the container. Every capability is
still dropped, so that owner has no extra kernel privileges, and the root file
system stays read-only. The step image is built with every setuid and setgid bit
removed, and `opendot verify-image` reports any such file it finds. This setting
is on by default; turn it off only when your Docker requires it.

## Files OpenDot writes

- The config file (`opendot init` writes it with mode 0600).
- The state folder (mode 0700): the SQLite database, run folders with step
  transcripts, CLI session folders and logs. Transcripts can contain task text
  and command output. Delete old run folders if you do not need them.

OpenDot masks the login values it holds and common secret formats (API keys,
bearer tokens, private key blocks) before it writes step transcripts and error
messages. This masking is a best effort, not a guarantee.
