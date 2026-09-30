# Contributing to OpenDot

Thank you for helping. This file says how to set up the code, what a change
must pass, and which rules keep the project safe.

## Set up

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Use uv for
every command; do not call `python` or `pip` directly.

```sh
git clone https://github.com/defog-ai/opendot && cd opendot
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

The tests need no Docker, no model login, no Slack and no network. They use the
scripted fake backend and fake command runners (see `tests/conftest.py`). To
try the whole flow by hand, follow the demo in the README.

## What a change must pass

- `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .`.
  CI runs them on Python 3.11, 3.12 and 3.13, and builds the step image.
- A test for each behaviour you add or fix.
- `tests/test_no_private_strings.py`. The repository is public: do not commit
  home-folder paths, personal email addresses, Slack member or channel ids,
  tokens or private keys. Use `example.com` addresses and ids that contain
  `EXAMPLE`.

## Code style

- Python 3.11 syntax, a `src/` layout, and the typing forms `list`, `dict` and
  `X | None`.
- Keep runtime dependencies to httpx, jsonschema, croniter and tzdata. Open an
  issue before you add one.
- Do not hard-code model names. The model comes from the config, and an empty
  value means the CLI's own default.
- Keep a pull request to one change. Do not mix in unrelated formatting.

## Rules for security-relevant code

These parts decide what the model can do. A change to them needs a test that
shows the old limit still holds:

- `sandbox.py` and `container_contract.py` (container flags and mounts);
- `actions/` and `github/actions.py` (which actions exist, and where each one
  goes);
- `github/public_text.py` (the check before a push to a public repository);
- `redact.py` (what reaches logs).

A change must not give the step container the Docker socket, the state folder,
the Slack token or host variables outside `sandbox.env_allowlist`. It must not
let the model choose the target of an action, and it must not let a stopped
task run more actions.

Report security problems by email to security@defog.ai, not in a public issue.
See [SECURITY.md](SECURITY.md).

## Writing

Write docs, messages and pull request descriptions in plain English. Say what
changed and why first, then the details. OpenAI's product is called "dots";
describe what OpenDot does, and do not make claims about the limits of dots.

## License

By contributing, you agree that your contribution is licensed under the Apache
License 2.0, as in [LICENSE](LICENSE).
