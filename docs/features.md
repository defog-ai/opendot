# Pull requests, the browser and connectors

OpenDot 0.2 has three features that are off until you turn them on. Each one
is one block in `opendot.toml`. Run `opendot doctor` after each change; it
checks the new block. The [README](../README.md) has short recipes, and
[how-it-works.md](how-it-works.md) explains how the host runs the actions that
the model proposes.

## Pull requests

**OpenDot can push a branch, open a pull request, open an issue and comment on
an issue. The model only edits files; the host commits, checks and publishes,
and each of these is an action that the host runs as soon as the model
proposes it.** It is off until you add a repository:

```toml
[github]
# The host reads the token from this variable. It never enters a container.
token_env = "OPENDOT_GITHUB_TOKEN"

[[repositories]]
name = "app"
remote = "https://github.com/example/app.git"
checks = ["uv run pytest -q"]
```

Make a fine-grained token that can write contents, pull requests and issues of
that repository only, and save it with `opendot login github`. The runs that
cron starts find the saved token. A set `OPENDOT_GITHUB_TOKEN` variable wins
over it. `opendot doctor` checks that git is installed, the token is saved or
set, and each remote is on GitHub.

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
4. **The host publishes without asking you.** A push, a pull request, an
   issue or a comment runs as soon as the model proposes it and the checks
   above pass. Before it pushes, the host checks that the copy still holds the
   prepared commit and tree, and that the repository is still public or private as it was. It
   never force-pushes and never pushes to the default branch.
5. **Retries do not publish twice.** Each pull request, issue and comment
   carries a hidden marker made from its content. A retry finds the earlier
   result through the marker, first in the database and then on GitHub. A new
   commit on a task that already has a pull request is pushed to the same
   pull request. A marked pull request, issue or comment counts only when the
   token's own account opened it, and for a pull request only when its branch
   is the task's branch in the same repository.

Commands: `opendot github repos`, `fetch`, `copies`, `publications`,
`prune` (deletes the copies of finished tasks) and `reconcile` (records pull
requests and issues that were merged or closed).

Limits:

- A check that needs the network needs `check_network` set to a Docker network
  that can reach it. The check container gets no token and no host variables.
- Files that `prepare` writes into the copy are committed unless the
  repository's `.gitignore` covers them.
- The text check matches patterns. It is a guard against mistakes, not a
  guarantee. Read the diff in `opendot show N` after the push.
- The task log shows the whole diff, as text, for every changed file. Git's
  diff drivers, text conversion and `.gitattributes` settings such as `-diff`
  cannot hide a line. A diff longer than `github.max_diff_chars` (20,000
  characters by default) is refused instead of cut.
- A binary file has no lines for the text check to read. In a public
  repository a change that adds or edits a binary file is refused unless
  `github.allow_binary_public = true`. In a private repository the task log
  lists each binary file and its size.
- An ssh remote uses the host user's ssh agent for fetch and push. An https
  remote uses the token.

**A repository that is not on GitHub can still get branches.** Set
`forge = "none"` on it and state its visibility. OpenDot then makes no GitHub
call and needs no token. Only `github.push_branch` works for it; pull
requests, issues and comments are refused. `github.author_email` is required,
because there is no GitHub account to take the commit author from.

```toml
[github]
author_email = "opendot@example.com"

[[repositories]]
name = "notes"
remote = "git@git.example.com:team/notes.git"   # or a local path to a bare repository
forge = "none"
visibility = "private"   # "private" or "public"; OpenDot cannot check this
```

The push runs as soon as the model proposes it, and the task log shows the
commit and the diff. With
`visibility = "public"`, the public text check applies, and `public = false`
refuses every action for the repository.

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
  (`browser_evaluate`, `browser_run_code_unsafe`), `browser_fill_form`, file
  upload and the cookie and storage tools. It includes `browser_type` and
  `browser_select_option`. Codex and opencode hide the other tools. Claude
  Code refuses them. The list limits the tools offered to the model; it does not limit the
  container. The model has a shell there and can start Chromium with
  Playwright from its own script.
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
  must be an action that the model proposes, and the host runs it without
  asking you.

Check the browser with `opendot verify-image`, which opens a local page with
no network, and with `opendot browser check --url https://example.com`, which
opens a real page with the sandbox settings from your config.

## Connectors and FactIQ

**Work steps can read from MCP servers ("connectors") through a gateway on the
host. The host holds the connector's key; the step container never sees it.**
Connectors are off until you list them in `opendot.toml`:

```toml
[[mcp_servers]]
name = "docs"
url = "https://mcp.example.com/mcp"   # http:// only for 127.0.0.1, localhost or ::1
auth = "bearer_env"                   # "none", "bearer_env" or "oauth"
auth_env = "DOCS_MCP_TOKEN"           # read on the host, for bearer_env
tools = [
  { name = "search", mode = "read" },
  { name = "create_note", mode = "write" },
]
```

- A connector can instead be a program: `command = ["some-mcp-server",
  "--stdio"]`. That program runs on the host as your user, outside the
  sandbox. OpenDot refuses such an entry unless it also has
  `allow_host_command = true`, and `opendot doctor` lists it.
- For each step the host starts one gateway per connector on a Unix socket in
  the step's run folder, and mounts that folder read-only at `/opendot/mcp`. A
  small script in the container relays the model's MCP client to the socket.
- The gateway lists and runs only the tools marked `read`. Any other tool name
  is refused. Every call is written to the database with its arguments, its
  status, its size and its duration. `opendot connectors calls` shows them.
- A tool marked `write` is not callable from the step. It becomes an action
  named `mcp.<server>.<tool>`. The model proposes it with the arguments, and
  the host calls the tool with those arguments at once, as it runs any other
  action. It does not ask you first.
- Large results are cut before they reach the model. A call that takes too
  long is stopped.
- `opendot connectors list`, `login <name>`, `logout <name>` and `test <name>`
  show the connectors, sign in to one that uses OAuth (the tokens are kept in
  the OpenDot database on the host) or save the key of one that uses
  `bearer_env` (in `core.keys_dir`, mode 600), remove a sign-in or a saved key,
  and check that the allowed tools exist on the server.

**FactIQ is built in as a preset.** [FactIQ](https://factiq.com) serves public
economic and financial data over MCP at `https://api.factiq.com/mcp`. Turn it
on when you create the config:

```sh
opendot init --with-factiq
```

or add it by hand:

```toml
[factiq]
enabled = true
# The other settings and their defaults:
# auth = "bearer_env"          # or "oauth", then: opendot connectors login factiq
# api_key_env = "FACTIQ_API_KEY"
# instructions = true
# feedback = false             # true adds send_feedback as an action
```

- With `auth = "bearer_env"`, create an API key in your FactIQ settings and
  save it with `opendot connectors login factiq`. A set `FACTIQ_API_KEY`
  variable wins over the saved key.
- The preset allows FactIQ's read tools. `send_feedback` is the only tool that
  sends something, and it is off unless `feedback = true`.
- With `instructions = true`, OpenDot downloads the skill and reference files
  from the public [factiq-plugin](https://github.com/defog-ai/factiq-plugin)
  repository at a fixed commit, keeps only the skill, reference and script
  files, and mounts them read-only at `/opendot/instructions/factiq`. The
  plugin's `LICENSE` (MIT) is kept next to them with a `SOURCE.md` that names
  the repository and commit. `opendot connectors fetch-instructions --force`
  downloads them again. If the download fails, steps run without them.
