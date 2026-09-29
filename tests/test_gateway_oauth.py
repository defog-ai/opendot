"""OAuth logins for connectors: token storage, refresh after a restart, sign-in helpers."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from opendot.config import McpServerConfig, McpToolConfig
from opendot.gateway.oauth import (
    LoginError,
    SignInNeeded,
    StoreTokenStorage,
    _CallbackServer,
    gateway_provider,
    has_login,
    login,
    parse_redirect,
)
from opendot.gateway.upstream import open_session

AUTH_METADATA = {
    "issuer": "https://auth.example.com/mcp",
    "authorization_endpoint": "https://auth.example.com/mcp/authorize",
    "token_endpoint": "https://auth.example.com/mcp/token",
    "registration_endpoint": "https://auth.example.com/mcp/register",
    "response_types_supported": ["code"],
}
CLIENT = {
    "client_id": "client-1",
    "client_secret": "secret-1",
    "redirect_uris": ["http://127.0.0.1:8765/callback"],
    "token_endpoint_auth_method": "client_secret_post",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}


def oauth_server() -> McpServerConfig:
    return McpServerConfig(
        name="remote",
        url="https://mcp.example.com/mcp",
        command=[],
        auth="oauth",
        auth_env="",
        tools=[McpToolConfig("search", "read")],
    )


def test_storage_keeps_client_info_until_the_first_token(store):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    storage = StoreTokenStorage(store, "remote", fresh=True)

    async def flow():
        await storage.set_client_info(OAuthClientInformationFull.model_validate(CLIENT))
        assert store.get_connector_token("remote") is None
        storage.save_extra(auth_server=AUTH_METADATA)
        await storage.set_tokens(
            OAuthToken(access_token="a1", token_type="Bearer", refresh_token="r1", expires_in=60)
        )

    anyio.run(flow)
    row = store.get_connector_token("remote")
    assert row.access_token == "a1" and row.refresh_token == "r1"
    assert row.client_info["client"]["client_id"] == "client-1"
    assert row.client_info["auth_server"]["token_endpoint"] == AUTH_METADATA["token_endpoint"]
    assert row.expires_at is not None
    assert has_login(oauth_server(), store)

    again = StoreTokenStorage(store, "remote")

    async def read():
        tokens = await again.get_tokens()
        info = await again.get_client_info()
        return tokens, info

    tokens, info = anyio.run(read)
    assert tokens.access_token == "a1"
    assert info.client_id == "client-1"


def test_expired_token_is_refreshed_at_the_stored_token_address(store):
    """After a restart the SDK alone would post the refresh to <origin>/token; the
    stored metadata sends it to the server's real token address."""
    import httpx2

    store.save_connector_token(
        "remote",
        "old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        scope="read",
        client_info={"client": CLIENT, "auth_server": AUTH_METADATA},
    )
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization", "")))
        if str(request.url) == AUTH_METADATA["token_endpoint"]:
            body = request.content.decode()
            assert "grant_type=refresh_token" in body
            assert "refresh_token=old-refresh" in body
            return httpx2.Response(
                200,
                json={
                    "access_token": "new-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "new-refresh",
                },
            )
        if str(request.url) == "https://mcp.example.com/mcp":
            return httpx2.Response(200, json={"ok": True})
        return httpx2.Response(404)

    provider = gateway_provider(oauth_server(), store)

    async def call():
        async with httpx2.AsyncClient(
            auth=provider, transport=httpx2.MockTransport(handler)
        ) as client:
            return await client.get("https://mcp.example.com/mcp")

    response = anyio.run(call)
    assert response.status_code == 200
    assert seen[0][1] == AUTH_METADATA["token_endpoint"]
    assert seen[-1] == ("GET", "https://mcp.example.com/mcp", "Bearer new-access")
    row = store.get_connector_token("remote")
    assert (row.access_token, row.refresh_token) == ("new-access", "new-refresh")
    assert row.expires_at > datetime.now(UTC)
    assert row.client_info["auth_server"]["token_endpoint"] == AUTH_METADATA["token_endpoint"]
    assert row.client_info["client"]["client_id"] == "client-1"


def test_valid_token_is_sent_without_a_refresh(store):
    import httpx2

    store.save_connector_token(
        "remote",
        "good-access",
        refresh_token="r",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        client_info={"client": CLIENT, "auth_server": AUTH_METADATA},
    )
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx2.Response(200)

    provider = gateway_provider(oauth_server(), store)

    async def call():
        async with httpx2.AsyncClient(
            auth=provider, transport=httpx2.MockTransport(handler)
        ) as client:
            await client.get("https://mcp.example.com/mcp")

    anyio.run(call)
    assert seen == ["Bearer good-access"]


def test_gateway_without_a_login_asks_for_one(store):
    async def connect():
        async with open_session(oauth_server(), store=store):
            pass

    with pytest.raises(SignInNeeded, match="opendot connectors login remote"):
        anyio.run(connect)


def test_parse_redirect():
    assert parse_redirect("http://127.0.0.1:5000/callback?code=abc&state=s1&x=y") == {
        "code": "abc",
        "state": "s1",
    }
    with pytest.raises(LoginError, match="access_denied"):
        parse_redirect("http://127.0.0.1/callback?error=access_denied")
    with pytest.raises(LoginError, match="code="):
        parse_redirect("http://127.0.0.1/callback?state=s1")


def test_callback_server_receives_the_redirect():
    with _CallbackServer(0) as callback:
        assert callback.redirect_uri.startswith("http://127.0.0.1:")

        def visit():
            urllib.request.urlopen(f"{callback.redirect_uri}?code=c1&state=s1", timeout=10).read()

        thread = threading.Thread(target=visit)
        thread.start()
        assert callback.wait(10) == {"code": "c1", "state": "s1"}
        thread.join()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(callback.redirect_uri.replace("/callback", "/x"), timeout=10)


def test_login_refuses_a_connector_without_oauth(store):
    server = McpServerConfig(
        name="keyed",
        url="https://mcp.example.com/mcp",
        command=[],
        auth="bearer_env",
        auth_env="KEYED_TOKEN",
        tools=[McpToolConfig("search", "read")],
    )
    with pytest.raises(LoginError, match="does not use oauth"):
        login(server, store, open_browser=False)


def test_login_that_stores_no_new_token_fails_even_with_an_old_one(store, monkeypatch):
    # An earlier sign-in is in the database. A server that answers without asking
    # for a sign-in must not make the new login look good.
    store.save_connector_token("remote", "old-access", token_type="Bearer", client_info={})

    async def answer_without_sign_in(server, provider):
        return ["search"]

    monkeypatch.setattr("opendot.gateway.upstream.list_remote_tools", answer_without_sign_in)
    with pytest.raises(LoginError, match="nothing was stored"):
        login(oauth_server(), store, open_browser=False, paste=True)
    assert store.get_connector_token("remote").access_token == "old-access"


def test_sdk_still_has_the_private_parts_the_provider_overrides():
    # HostOAuthProvider overrides private methods of the MCP SDK and sets fields
    # on its context. pyproject.toml pins the SDK's minor version; this test
    # fails first when a new SDK moves them.
    import inspect

    from mcp.client.auth import OAuthClientProvider
    from mcp.client.auth.oauth2 import OAuthContext

    for name in ("_initialize", "_handle_token_response", "_handle_refresh_response"):
        assert inspect.iscoroutinefunction(getattr(OAuthClientProvider, name)), name
    fields = set(getattr(OAuthContext, "__dataclass_fields__", {})) or set(
        inspect.signature(OAuthContext).parameters
    )
    for name in (
        "oauth_metadata",
        "protected_resource_metadata",
        "auth_server_url",
        "current_tokens",
        "token_expiry_time",
    ):
        assert name in fields, name
