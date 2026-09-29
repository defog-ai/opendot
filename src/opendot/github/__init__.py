"""Pull requests, issues and comments on GitHub (v0.2).

Each task gets its own copy of each configured repository. The model edits files
in that copy inside its step container; the copy's .git folder is read-only
there, so the model cannot commit, push or change remotes. The host:

- keeps a control clone per repository (repos/<name>) and fetches it;
- makes the task's copy with `git clone --local` (worktrees/task-<id>/<name>);
- commits the model's edits, runs the repository's checks in a sandbox
  container and records the tree hash, the change list and the check results in
  the action's payload (github.actions);
- pushes and calls the GitHub REST API only when the action runs, after rules,
  review and approval.

Modules:
- git: host git commands with hooks, fsmonitor, host config and submodules off.
- api: a small GitHub REST client over httpx.
- containers: repository commands (prepare, checks) in a sandbox container.
- public_text: the private-text check for public repositories.
- actions: the four action handlers.
- step: the step extension that makes and mounts the copies.
- cli: `opendot github ...` and the doctor checks.
"""
