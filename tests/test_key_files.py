"""Saved logins for Slack, GitHub and connectors, and the commands that save them."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

import opendot.__main__ as cli
from opendot.config import Config
from opendot.key_files import (
    KeyFileError,
    key_path,
    key_source,
    load_config,
    needed_keys,
    read_key_file,
    save_key_file,
    set_saved_keys,
)
from test_cli import FakeRunner

SLACK_TOKEN = "xoxb-1234-5678-abcdefghijklmnop"
GITHUB_TOKEN = "github_pat_" + "a" * 40
FACTIQ_KEY = "fiq_" + "b" * 32

SLACK_AND_GITHUB = """
[channels.slack]
enabled = true
channels = ["C123"]
allowed_users = ["U123"]

[[repositories]]
name = "widget"
remote = "https://github.com/acme/widget.git"
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    for name in list(os.environ):
        if name.startswith("OPENDOT_") or name == "FACTIQ_API_KEY":
            monkeypatch.delenv(name, raising=False)
    return tmp_path


def run(home: Path, *argv: str) -> int:
    return cli.main(["--config", str(home / "opendot.toml"), *argv])


def setup(home: Path, extra: str = SLACK_AND_GITHUB) -> Config:
    assert run(home, "init", "--demo", "--state-root", str(home / "state")) == 0
    with open(home / "opendot.toml", "a", encoding="utf-8") as out:
        out.write(extra)
    return Config.load(home / "opendot.toml")


def type_in(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text + "\n"))


# -- the files ---------------------------------------------------------------


def test_save_and_read_a_key_file(tmp_path):
    path = tmp_path / "keys" / "OPENDOT_SLACK_BOT_TOKEN"
    save_key_file(path, SLACK_TOKEN)
    assert read_key_file(path) == SLACK_TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_missing_key_file_reads_as_none(tmp_path):
    assert read_key_file(tmp_path / "NOPE") is None


def test_refuses_a_key_file_that_others_can_read(tmp_path):
    path = tmp_path / "KEY"
    save_key_file(path, "value")
    os.chmod(path, 0o640)
    with pytest.raises(KeyFileError, match="chmod 600"):
        read_key_file(path)


def test_refuses_a_key_file_that_is_a_link(tmp_path):
    target = tmp_path / "target"
    save_key_file(target, "value")
    link = tmp_path / "KEY"
    link.symlink_to(target)
    with pytest.raises(KeyFileError, match="not a regular file"):
        read_key_file(link)


@pytest.mark.parametrize("name", ["../secret", "A/B", "", "1ABC", "A B"])
def test_a_key_file_is_named_only_by_a_variable_name(tmp_path, name):
    with pytest.raises(KeyFileError, match="not a variable name"):
        key_path(tmp_path, name)


# -- setting the variables ---------------------------------------------------


def test_saved_keys_fill_only_the_variables_that_are_not_set(home):
    config = setup(home)
    keys = config.core.keys_dir
    save_key_file(keys / "OPENDOT_SLACK_BOT_TOKEN", SLACK_TOKEN)
    save_key_file(keys / "OPENDOT_GITHUB_TOKEN", GITHUB_TOKEN)
    save_key_file(keys / "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-not-used-here")
    environ = {"OPENDOT_GITHUB_TOKEN": "from-the-shell"}
    assert set_saved_keys(config, environ) == ["OPENDOT_SLACK_BOT_TOKEN"]
    assert environ == {
        "OPENDOT_GITHUB_TOKEN": "from-the-shell",
        "OPENDOT_SLACK_BOT_TOKEN": SLACK_TOKEN,
    }


def test_an_unreadable_key_file_is_skipped(home):
    config = setup(home)
    path = config.core.keys_dir / "OPENDOT_SLACK_BOT_TOKEN"
    save_key_file(path, SLACK_TOKEN)
    os.chmod(path, 0o644)
    environ: dict[str, str] = {}
    assert set_saved_keys(config, environ) == []
    assert key_source(config, "OPENDOT_SLACK_BOT_TOKEN", environ)[0] == "error"


def test_load_config_sets_saved_keys_in_the_process(home, monkeypatch):
    config = setup(home)
    save_key_file(config.core.keys_dir / "OPENDOT_SLACK_BOT_TOKEN", SLACK_TOKEN)
    loaded = load_config(home / "opendot.toml")
    assert os.environ["OPENDOT_SLACK_BOT_TOKEN"] == SLACK_TOKEN
    assert loaded.keys_from_files == ("OPENDOT_SLACK_BOT_TOKEN",)
    assert loaded.slack.bot_token() == SLACK_TOKEN
    source, detail = key_source(loaded, "OPENDOT_SLACK_BOT_TOKEN", os.environ)
    assert source == "file"


def test_needed_keys_follow_the_features_that_are_on(home):
    config = setup(home, SLACK_AND_GITHUB + "\n[factiq]\nenabled = true\n")
    assert needed_keys(config) == [
        ("OPENDOT_SLACK_BOT_TOKEN", "opendot login slack"),
        ("OPENDOT_GITHUB_TOKEN", "opendot login github"),
        ("FACTIQ_API_KEY", "opendot connectors login factiq"),
    ]
    assert needed_keys(setup(home / "plain", "")) == []


# -- commands ----------------------------------------------------------------


def test_login_slack_saves_the_token(home, monkeypatch, capsys):
    config = setup(home)
    type_in(monkeypatch, SLACK_TOKEN)
    capsys.readouterr()
    assert run(home, "login", "slack") == 0
    path = config.core.keys_dir / "OPENDOT_SLACK_BOT_TOKEN"
    assert read_key_file(path) == SLACK_TOKEN
    out = capsys.readouterr().out
    assert f"Saved OPENDOT_SLACK_BOT_TOKEN in {path}" in out
    assert "runs that cron starts" in out


def test_login_slack_refuses_a_token_that_is_not_a_bot_token(home, monkeypatch, capsys):
    config = setup(home)
    type_in(monkeypatch, "xoxp-user-token")
    assert run(home, "login", "slack") == 1
    assert "starts with xoxb-" in capsys.readouterr().err
    assert not (config.core.keys_dir / "OPENDOT_SLACK_BOT_TOKEN").exists()


def test_login_replaces_a_saved_token(home, monkeypatch):
    config = setup(home)
    save_key_file(config.core.keys_dir / "OPENDOT_GITHUB_TOKEN", "old-token")
    type_in(monkeypatch, GITHUB_TOKEN)
    assert run(home, "login", "github") == 0
    assert read_key_file(config.core.keys_dir / "OPENDOT_GITHUB_TOKEN") == GITHUB_TOKEN


def test_doctor_says_where_each_login_is(home, monkeypatch):
    config = setup(home)
    save_key_file(config.core.keys_dir / "OPENDOT_GITHUB_TOKEN", GITHUB_TOKEN)
    env = {"OPENDOT_SLACK_BOT_TOKEN": SLACK_TOKEN}
    findings = cli.doctor_findings(config, FakeRunner(), env=env)
    github_path = config.core.keys_dir / "OPENDOT_GITHUB_TOKEN"
    assert ("ok", f"OPENDOT_GITHUB_TOKEN is saved in {github_path}") in findings
    assert (
        "warn",
        "OPENDOT_SLACK_BOT_TOKEN is set here but not saved; cron does not read your shell "
        "profile, so runs that cron starts may not see it. Run `opendot login slack`",
    ) in findings
    findings = cli.doctor_findings(config, FakeRunner(), env={})
    assert (
        "error",
        "OPENDOT_SLACK_BOT_TOKEN is not set and not saved; run `opendot login slack`",
    ) in findings
    slack_path = config.core.keys_dir / "OPENDOT_SLACK_BOT_TOKEN"
    save_key_file(slack_path, SLACK_TOKEN)
    findings = cli.doctor_findings(config, FakeRunner(), env=env)
    assert ("ok", f"OPENDOT_SLACK_BOT_TOKEN is saved in {slack_path}") in findings
    assert not [m for level, m in findings if level == "warn" and "SLACK" in m]


def test_install_cron_names_the_logins_cron_cannot_see(home, monkeypatch, capsys):
    setup(home)
    monkeypatch.setattr(cli, "_runner", lambda: FakeRunner())
    monkeypatch.setenv("OPENDOT_SLACK_BOT_TOKEN", SLACK_TOKEN)
    capsys.readouterr()
    assert run(home, "install-cron") == 0
    out = capsys.readouterr().out
    assert "cannot see OPENDOT_SLACK_BOT_TOKEN. Run `opendot login slack`" in out
    assert "cannot see OPENDOT_GITHUB_TOKEN. Run `opendot login github`" in out

    monkeypatch.delenv("OPENDOT_SLACK_BOT_TOKEN")
    type_in(monkeypatch, SLACK_TOKEN)
    assert run(home, "login", "slack") == 0
    type_in(monkeypatch, GITHUB_TOKEN)
    assert run(home, "login", "github") == 0
    capsys.readouterr()
    assert run(home, "install-cron") == 0
    assert "cannot see" not in capsys.readouterr().out


def test_connectors_login_saves_an_api_key_and_logout_removes_it(home, monkeypatch, capsys):
    config = setup(home, "\n[factiq]\nenabled = true\ninstructions = false\n")
    type_in(monkeypatch, FACTIQ_KEY)
    assert run(home, "connectors", "login", "factiq") == 0
    path = config.core.keys_dir / "FACTIQ_API_KEY"
    assert read_key_file(path) == FACTIQ_KEY
    monkeypatch.delenv("FACTIQ_API_KEY", raising=False)
    capsys.readouterr()
    assert run(home, "connectors", "list") == 0
    assert f"login: ready (API key: FACTIQ_API_KEY is saved in {path})" in capsys.readouterr().out
    assert run(home, "connectors", "logout", "factiq") == 0
    assert not path.exists()
