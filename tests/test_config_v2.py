"""v0.2 configuration: repositories, GitHub, browser, connectors and FactIQ."""

import pytest

from opendot.config import (
    FACTIQ_READ_TOOLS,
    FACTIQ_URL,
    Config,
    ConfigError,
    parse_github_remote,
)

V01_CONFIG = {
    "core": {"state_root": "/tmp/opendot-test", "timezone": "UTC"},
    "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
    "sandbox": {"network": "none", "env_allowlist": ["LANG"]},
    "rules": [{"kind": "note.write", "level": "ask"}],
}


def test_v01_config_still_loads_with_safe_defaults():
    cfg = Config.from_dict(V01_CONFIG, env={})
    assert cfg.sandbox.no_new_privileges is True
    assert cfg.repositories == []
    assert cfg.mcp_servers == []
    assert cfg.browser.enabled is False
    assert cfg.factiq.enabled is False
    assert cfg.connectors() == []


def test_no_new_privileges_can_be_turned_off():
    cfg = Config.from_dict({"sandbox": {"no_new_privileges": False}}, env={})
    assert cfg.sandbox.no_new_privileges is False
    cfg = Config.from_dict({}, env={"OPENDOT_SANDBOX_NO_NEW_PRIVILEGES": "false"})
    assert cfg.sandbox.no_new_privileges is False


def test_repositories_parse_and_default():
    cfg = Config.from_dict(
        {
            "repositories": [
                {"name": "app", "remote": "https://github.com/example/app.git"},
                {
                    "name": "site",
                    "remote": "git@" + "github.com:example/site.git",
                    "default_branch": "trunk",
                    "prepare": ["npm ci"],
                    "checks": ["npm test"],
                    "public": True,
                },
            ]
        },
        env={},
    )
    app = cfg.repository("app")
    assert app.default_branch == "main"
    assert app.public is False
    assert app.check_network == "none"
    assert app.github_slug("github.com") == ("example", "app")
    site = cfg.repository("site")
    assert site.checks == ["npm test"]
    assert site.github_slug("github.com") == ("example", "site")
    assert cfg.repository("missing") is None


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("https://github.com/example/app", ("example", "app")),
        ("https://github.com/example/app.git/", ("example", "app")),
        ("ssh://git@" + "github.com/example/app.git", ("example", "app")),
        ("https://gitlab.example.com/example/app.git", None),
        ("/srv/git/app.git", None),
    ],
)
def test_parse_github_remote(remote, expected):
    assert parse_github_remote(remote, "github.com") == expected


@pytest.mark.parametrize(
    "repos",
    [
        [{"name": "a.b", "remote": "x"}],
        [{"name": "a__b", "remote": "x"}],
        [{"name": "a", "remote": "x"}, {"name": "a", "remote": "y"}],
        [{"name": "a", "remote": "-x"}],
        [{"name": "a", "remote": "x", "unknown": 1}],
        [{"name": "a", "remote": "x", "public": "yes"}],
        [{"name": "a", "remote": "x", "check_network": "host"}],
    ],
)
def test_bad_repositories_are_refused(repos):
    with pytest.raises(ConfigError):
        Config.from_dict({"repositories": repos}, env={})


def test_mcp_servers_parse():
    cfg = Config.from_dict(
        {
            "mcp_servers": [
                {
                    "name": "docs",
                    "url": "https://mcp.example.com/mcp",
                    "auth": "bearer_env",
                    "auth_env": "DOCS_TOKEN",
                    "tools": [
                        {"name": "search", "mode": "read"},
                        {"name": "create_page", "mode": "write"},
                    ],
                },
                {
                    "name": "local",
                    "command": ["srv", "--stdio"],
                    "tools": [{"name": "t", "mode": "read"}],
                },
            ]
        },
        env={},
    )
    docs = cfg.connector("docs")
    assert docs.read_tools == ["search"]
    assert docs.write_tools == ["create_page"]
    assert docs.token({"DOCS_TOKEN": "abc"}) == "abc"
    assert cfg.connector("local").auth == "none"
    assert "DOCS_TOKEN" in cfg.secret_env_names()


@pytest.mark.parametrize(
    "server",
    [
        {
            "name": "browser",
            "url": "https://a.example.com",
            "tools": [{"name": "t", "mode": "read"}],
        },
        {"name": "a", "tools": [{"name": "t", "mode": "read"}]},
        {
            "name": "a",
            "url": "https://a.example.com",
            "command": ["x"],
            "tools": [{"name": "t", "mode": "read"}],
        },
        {"name": "a", "url": "http://a.example.com", "tools": [{"name": "t", "mode": "read"}]},
        {"name": "a", "url": "https://a.example.com", "tools": []},
        {"name": "a", "url": "https://a.example.com", "tools": [{"name": "t", "mode": "admin"}]},
        {"name": "a", "url": "https://a.example.com", "tools": [{"name": "t.x", "mode": "read"}]},
        {
            "name": "a",
            "url": "https://a.example.com",
            "auth": "bearer_env",
            "tools": [{"name": "t", "mode": "read"}],
        },
        {"name": "a", "command": ["x"], "auth": "oauth", "tools": [{"name": "t", "mode": "read"}]},
    ],
)
def test_bad_mcp_servers_are_refused(server):
    with pytest.raises(ConfigError):
        Config.from_dict({"mcp_servers": [server]}, env={})


def test_factiq_preset():
    cfg = Config.from_dict({"factiq": {"enabled": True}}, env={})
    [factiq] = cfg.connectors()
    assert factiq.name == "factiq"
    assert factiq.url == FACTIQ_URL
    assert factiq.read_tools == list(FACTIQ_READ_TOOLS)
    assert factiq.write_tools == []
    assert factiq.auth_env == "FACTIQ_API_KEY"
    assert factiq.instructions == cfg.connectors_dir / "factiq" / "instructions"
    assert "FACTIQ_API_KEY" in cfg.secret_env_names()

    with_feedback = Config.from_dict({"factiq": {"enabled": True, "feedback": True}}, env={})
    assert with_feedback.connector("factiq").write_tools == ["send_feedback"]

    oauth = Config.from_dict({"factiq": {"enabled": True, "auth": "oauth"}}, env={})
    assert oauth.connector("factiq").auth_env == ""


def test_factiq_name_clash_is_refused():
    server = {
        "name": "factiq",
        "url": "https://a.example.com",
        "tools": [{"name": "t", "mode": "read"}],
    }
    with pytest.raises(ConfigError):
        Config.from_dict({"factiq": {"enabled": True}, "mcp_servers": [server]}, env={})


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"github": {"token_env": "MY_GH"}},
        {"factiq": {"api_key_env": "MY_FACTIQ"}},
    ],
)
def test_secret_names_are_refused_in_env_allowlist(extra):
    name = (
        extra.get("github", {}).get("token_env")
        or extra.get("factiq", {}).get("api_key_env")
        or "OPENDOT_GITHUB_TOKEN"
    )
    with pytest.raises(ConfigError):
        Config.from_dict({**extra, "sandbox": {"env_allowlist": [name]}}, env={})


def test_connector_token_name_is_refused_in_env_allowlist():
    server = {
        "name": "docs",
        "url": "https://a.example.com",
        "auth": "bearer_env",
        "auth_env": "DOCS_TOKEN",
        "tools": [{"name": "t", "mode": "read"}],
    }
    with pytest.raises(ConfigError):
        Config.from_dict(
            {"mcp_servers": [server], "sandbox": {"env_allowlist": ["DOCS_TOKEN"]}}, env={}
        )


def test_browser_and_github_validation():
    with pytest.raises(ConfigError):
        Config.from_dict({"browser": {"viewport": "big"}}, env={})
    with pytest.raises(ConfigError):
        Config.from_dict({"github": {"api_url": "http://api.example.com"}}, env={})
    cfg = Config.from_dict({"github": {"signing_key": "~/.ssh/signing"}}, env={})
    assert cfg.github.signing_key.name == "signing"
    assert cfg.github.token({"OPENDOT_GITHUB_TOKEN": "t"}) == "t"


def test_ensure_directories_creates_v02_folders(tmp_path):
    cfg = Config.from_dict({"core": {"state_root": str(tmp_path / "state")}}, env={})
    cfg.ensure_directories()
    for path in (cfg.repos_dir, cfg.worktrees_dir, cfg.connectors_dir):
        assert path.is_dir()
        assert path.stat().st_mode & 0o777 == 0o700
