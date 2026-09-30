# Security

OpenDot runs model-written commands on your machine inside Docker containers.
The containers limit what those commands can reach, but they do not make them
safe. A step container has open network access by default, and it holds the
model login it needs. Treat anything the step container can reach as
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
- the GitHub token and the host user's ssh agent;
- connector keys and connector OAuth tokens;
- other people's notes;
- the Docker socket;
- host environment variables, unless you name them in `sandbox.env_allowlist`
  (work steps only; reflect steps never get any). The allowlist may not name the
  Slack token variable, the Claude Code login variable, the GitHub token
  variable or any connector key variable.

### Saved logins

`opendot login slack`, `opendot login github` and `opendot connectors login
<name>` (for a connector with `auth = "bearer_env"`) save a token in
`core.keys_dir` (default `~/.config/opendot/keys`). Each token is one file,
named after its variable, with mode 600, in a folder with mode 700. When an
`opendot` command loads its config, it sets each of these variables that is not
set from its file. A variable that is set wins over the file.

- OpenDot refuses to read a saved file that another user can read, that belongs
  to another user or that is a symbolic link. `opendot doctor` reports it.
- The file name must be a plain variable name, so a config value cannot point
  the file outside the folder.
- The saved values reach the same places as the variables do. The rules above
  still apply: no step container gets them, and the allowlist may not name them.
- The files are not encrypted. Anyone who can read files as your user, or as
  root, can read them. The same is true of a line in a shell profile.

The model cannot run an action by itself. It proposes actions in its JSON
output. The host refuses unknown action kinds and builds each action's target
from the task (not from the model's text). Then it runs the action at once. The
host does not ask you first, and no second model checks the action.

Each step container:

- starts fresh with `docker run --rm`;
- drops every Linux capability and, unless `sandbox.no_new_privileges` is
  false, sets `no-new-privileges`;
- has a read-only root file system, with size-limited tmpfs folders for `/tmp`
  and the home folder;
- runs as your user id (or a fixed non-root user when you run OpenDot as root);
- has CPU, memory and process limits;
- mounts only its work folder and its CLI session folder, plus any read-only
  folders you list in `sandbox.readonly_mounts`.

Work steps can get more mounts from the features you turn on. Reflect steps
never get them. Each one comes from a fixed folder under the state folder:

| Place in the container | Mode | When |
| --- | --- | --- |
| `/opendot/repos/<name>` | writable, with its `.git` folder read-only on top | `[[repositories]]` is set |
| `/opendot/run/artifacts` | writable | the browser is on |
| `/opendot/mcp` | read-only (the gateway sockets) | a connector is on |
| `/opendot/instructions/<name>` | read-only | a connector shares instruction files |

Text the model writes into a repository copy or the artifacts folder stays on
the host after the step. The host reads a repository copy only to build a
proposed commit, which goes through your checks and the public text check.

## What the host does not protect

Read this section before you give OpenDot real work.

### Network access

The default Docker network is `bridge`. A step container can connect to any
address your machine can reach: the internet, your local network, and services
that listen on the Docker bridge address of your machine. The model CLIs need
this to reach their own API.

If you set `sandbox.network = "none"`, the container has no network, and the
model CLIs cannot reach their API, so steps fail. You can create
your own Docker network that only allows the model API (for example, through an
egress proxy) and put its name in `sandbox.network`. OpenDot does not ship such
a network. `network = "host"` and `network = "container:<name>"` are
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
- **Claude Code:** OpenDot passes the token into the container. It takes the
  token from the variable named in `backend.claude_code.token_env` (default
  `CLAUDE_CODE_OAUTH_TOKEN`) or, when that variable is not set, from the file
  `backend.claude_code.token_file` (default `~/.config/opendot/claude-token`)
  that `opendot login claude` writes with mode 600. OpenDot refuses to read
  that file when another user can read it or when it is a symbolic link.
- **opencode:** OpenDot copies only the entry for the provider in the model
  name from your opencode login file (`backend.opencode.auth_file`) into the
  session folder, and deletes it when the step ends. A refreshed OAuth login is
  copied back only when it keeps the same fields and your own file did not
  change during the step. A changed API key is never copied back.

Code that runs in the container can read that login and, with network access,
send it elsewhere. Use a login that you can revoke, and revoke it if you think a
step was misused. Prefer a token from `claude setup-token` or an API key with a
spending limit over your main account password.

### Every proposed action runs

OpenDot has no approvals, no rules and no reviewer. When the model proposes a
reply, a note, a schedule, a push, a pull request, an issue, a comment or a
connector `write` call, the host runs it as soon as the step ends. A model that
reads hostile text (a web page, a file, a connector result) can be led to
propose one of these actions, and it will run.

What still limits an action:

- the host runs only the action kinds it knows, and builds each target itself;
- a push needs your repository checks to pass, never goes to the default
  branch and, for a public repository, must pass the public text check;
- a connector tool runs only when you marked it `write` in your config;
- `opendot stop N` stops a task, and the host runs none of its remaining
  actions.

Connect only the repositories, channels and connector `write` tools that you
accept this for.

### Who may give work

On Slack, only the member ids in `channels.slack.allowed_users` can create
tasks, and an empty list allows nobody. Messages from other people in a task's thread are not given
to the model. Text that the requester pastes, and files or pages that the model
reads, can still hold hostile instructions.

A follow-up from a different allowed user in a finished thread starts a new
model session with that user's own notes. It does not continue the earlier
requester's session.

### Notes the model writes

A note is read by every later task for the same requester, and the host saves
every note the model proposes. When the proposal cites, as its source, a
message that the requester wrote in the current task, the host checks that the
message belongs to this task and to this requester, and records the note as
coming from the requester. Otherwise it records the note as coming from the
model. It does not check that the note text matches the message, because a note
is the model's summary of it. `opendot notes` lists every note, and you can
change or remove any of them.
On the command line, anyone who can run `opendot` as your user is the operator.

### Docker itself

A user who can run Docker commands can become root on most machines. OpenDot
needs Docker, so the user that runs OpenDot has that power. Rootless Docker is
untested.

### The no_new_privileges trade-off

`sandbox.no_new_privileges` is true by default. Keep it true unless your Docker
refuses it.

Docker from a snap package refuses containers started with
`no-new-privileges`. To use snap Docker, set `sandbox.no_new_privileges = false`
and keep the state folder and every read-only mount outside `/tmp`, because
snap Docker cannot see mount sources there. `opendot doctor` detects snap
Docker. It reports an error while the setting is true, and an ok line when the
setting is false and no mounted folder is under `/tmp`.

What you lose when the setting is false: a setuid or setgid program inside the
container could run with the user id of its owner (usually root) inside the
container. What still holds:

- every Linux capability is dropped, so that user has no extra kernel
  privileges;
- the root file system stays read-only;
- the step image is built with every setuid and setgid bit removed, and
  `opendot verify-image` reports any such file it finds;
- the container still runs as your user id, with the same mounts and limits.

The risk grows if you use your own image that keeps setuid programs. Run
`opendot verify-image` on any image you use with this setting off.

## Pull requests and the GitHub token

The GitHub token never enters a container. The model edits files in its copy
of the repository; the `.git` folder of that copy is read-only in the
container, so the model cannot commit, push or change the remote. The host
commits, runs the checks in a separate container with no token, no host
variables and the repository's `check_network` (`none` by default), and pushes
only when the checks pass. It does not ask you before it pushes.

- **Use a fine-grained token.** Give it write access to contents, pull
  requests and issues of the listed repositories only. Revoke it if you think
  the host machine was misused.
- **ssh remotes use your ssh agent.** For an ssh remote, the host runs `git`
  with the host user's ssh agent. That agent can reach every repository your
  key can reach. OpenDot pushes only to the configured remote and the task's
  own branch, never to the default branch, and never force-pushes.
- **The public text check is a best effort.** For a public repository the host
  refuses an action whose added lines, file names, commit messages, title or
  body contain a home-folder path, an email address outside the example
  domains, text shaped like a token, a link to a coding session or one of your
  `github.private_markers`. It matches patterns. It does not find every secret
  or every private fact, and nobody reads the diff before the push.
- **The diff in the task log is the whole change.** The host records every text
  line of the change and does not use git's diff drivers, text conversion or
  `.gitattributes` to shorten it. When the change edits `.gitattributes`, the
  log shows the new file. A change whose diff is longer than
  `github.max_diff_chars` (20,000 characters by default) is refused, so the
  log never holds a cut diff and the public text check reads every line. Ask
  for smaller commits, or raise the limit.
- **Binary files are refused for a public repository.** Git shows no lines for
  a binary file, so the public text check cannot read it. A change that adds or
  edits a binary file in a public repository is refused unless you set
  `github.allow_binary_public = true`. For a private repository the task log
  lists each binary file with its size.
- **The host only takes over its own pull requests, issues and comments.** A
  pull request, issue or comment that another account opened is never edited,
  even when it carries OpenDot's marker or uses the task's branch.
- **Checks run the repository's own commands.** A check container gets no token
  and, by default, no network. Code in the repository or in the model's edit
  still runs there, so a check network that can reach the internet can send
  the repository's contents elsewhere.
- **`forge = "none"` visibility is your statement.** For a plain git remote the
  host cannot ask a server whether the repository is public. It trusts the
  `visibility` you set. If you set `private` on a public remote, the public
  text check does not run.
- **Pushes, pull requests, issues and comments do not ask.** The host runs
  them as soon as the model proposes them and the checks above pass.

## The browser

The browser is off by default. When it is on, a headless Chromium runs inside
the work step's container and nowhere else.

- **The network is the boundary.** The browser uses the step container's
  network (`sandbox.network`). With the default `bridge` network it can open
  any address your machine can reach, including services on your local network.
  `browser.allowed_origins` is passed to Playwright, but it does not cover
  redirects and is not a security boundary.
- **Chromium's own sandbox is off.** The container drops every Linux
  capability, and Chromium cannot build its sandbox without them. The
  container is the boundary.
- **No logins.** The browser starts with an empty profile in memory: no
  cookies, no saved passwords and no host credentials. Nothing is kept between
  steps.
- **Page text is untrusted.** A web page can hold instructions aimed at the
  model. The model is told to treat page text as data. Anything outward must
  be an action that the model proposes, and the host runs it without asking
  you. A page can
  still lead the model to open other pages, which sends data in the address.
- **The tool list limits what the model is offered, not what the container
  can do.** The default `browser.tools` list leaves out `browser_evaluate`,
  `browser_run_code_unsafe`, `browser_fill_form`, file upload and the cookie
  and storage tools. It includes `browser_type` and `browser_select_option`, so
  the model can type into a page's fields. The model also has a shell in the
  same container, where Chromium and Playwright are installed. It can start
  its own browser from a script, with any page script, any form and any
  address the network allows. Treat a work step with the browser on as a
  step that can reach every address on `sandbox.network`.
- **Artifacts stay on disk.** Screenshots and page snapshots are saved in the
  step's run folder under the state folder. The artifacts folder is writable
  from the container, so it can also hold any file the model writes there.
  Delete old run folders if they hold pages you do not want to keep.

## Connectors and the gateway

Connectors are off until you list them. The step container never gets a
connector key.

- **A command connector runs on the host.** A `[[mcp_servers]]` entry with
  `command` starts that program on the host as your user, outside the
  sandbox, with your files and network. OpenDot refuses such an entry unless
  it also has `allow_host_command = true`, and `opendot doctor` lists each
  one. Use a `url` connector when you can.
- **Connector addresses use https.** A `url` must be `https://`. Plain
  `http://` is accepted only for `127.0.0.1`, `localhost` and `::1`.
- **Keys stay on the host.** For each step the host starts one gateway per
  connector. The gateway listens on a Unix socket in the step's run folder,
  which is mounted read-only in the container. The gateway reads the key from
  the host variable you name (or from the OAuth tokens kept in the OpenDot
  database) and adds it to each request it sends to the connector.
- **Only read tools are callable from a step.** The gateway lists and runs only
  the tools you mark `read`, and refuses every other name. The server decides
  what a tool does; `read` is your statement about the tool. Mark a tool `read`
  only when you are sure it changes nothing.
- **Write tools become actions.** A tool marked `write` is an action named
  `mcp.<server>.<tool>`. The host calls it with the model's arguments as soon
  as the model proposes it. It does not ask you first. Mark a tool `write`
  only when you accept that the model can call it at any time.
- **Every call is logged.** The database records each call's tool, arguments,
  status, result size and duration. `opendot connectors calls` shows them.
- **Results are capped.** A result larger than `gateway.max_result_kib` is cut
  before the model sees it, and a call that runs longer than
  `gateway.call_timeout_seconds` is stopped.
- **Connector results are untrusted.** Text from a connector is data, the same
  as a web page.
- **Errors do not show keys.** Before the gateway shows an error or a server's
  instructions to the model, it masks the connector's key, its stored OAuth
  tokens and any values in the query part of its address.
- **Read tools can still carry data out.** The arguments of a read call go to
  the connector's server. A misled model could put text from the task into a
  search query. Use connectors you trust with the task's data.
- **The FactIQ preset** talks to `factiq.url`, which is
  `https://api.factiq.com/mcp` by default and must use https. Its
  `send_feedback` tool is off unless `factiq.feedback = true`, and then it is an
  action that the host runs when the model proposes it. The plugin files are downloaded from a fixed commit
  of the public factiq-plugin repository and mounted read-only.

## Files OpenDot writes

- The config file (`opendot init` writes it with mode 0600).
- The state folder (mode 0700): the SQLite database, run folders with step
  transcripts, CLI session folders and logs. Transcripts can contain task text
  and command output. Delete old run folders if you do not need them.

OpenDot masks the login values it holds and common secret formats (API keys,
bearer tokens, private key blocks) before it writes step transcripts and error
messages. This masking is a best effort, not a guarantee.
