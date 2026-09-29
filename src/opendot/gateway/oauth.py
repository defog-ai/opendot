"""OAuth sign-in for connectors whose auth is "oauth".

`opendot connectors login <name>` runs the authorization-code flow once on the
host and keeps the token in the connector_tokens table. The gateway then uses the
stored token and refreshes it when it expires. The gateway never opens a browser:
when the stored login cannot be used, the call fails with a message that says to
sign in again.

The mcp SDK's OAuthClientProvider keeps the authorization server's addresses and
the token's expiry time in memory only. HostOAuthProvider saves both with the
token and restores them, so a refresh after a restart goes to the server's real
token address.
"""

from __future__ import annotations

import http.server
import queue
import threading
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from opendot.config import McpServerConfig
    from opendot.store import Store

__all__ = [
    "CLIENT_NAME",
    "LoginError",
    "SignInNeeded",
    "StoreTokenStorage",
    "gateway_provider",
    "login",
    "parse_redirect",
]

CLIENT_NAME = "OpenDot"
CALLBACK_PATH = "/callback"
LOGIN_TIMEOUT_SECONDS = 300


class SignInNeeded(RuntimeError):
    """The stored login is missing or cannot be refreshed."""


class LoginError(RuntimeError):
    """The sign-in did not finish."""


def _sign_in_message(server: str) -> str:
    return (
        f"connector {server} needs a sign-in: run `opendot connectors login {server}` on the host"
    )


class StoreTokenStorage:
    """The SDK's TokenStorage, backed by the connector_tokens table.

    The row's client_info column holds a JSON object with three keys: client (the
    registered client), auth_server (the authorization server metadata) and
    resource (the protected resource metadata). Client information that arrives
    before the first token is kept in memory until the token is saved.

    The Store must belong to the thread that runs the event loop.
    """

    def __init__(self, store: Store, server: str, *, fresh: bool = False):
        self.store = store
        self.server = server
        self.fresh = fresh  # ignore what is stored (a new sign-in)
        self._pending: dict[str, Any] = {}

    def _row(self):
        return None if self.fresh else self.store.get_connector_token(self.server)

    def stored_extra(self) -> dict[str, Any]:
        row = self._row()
        extra = dict(row.client_info) if row is not None else {}
        extra.update(self._pending)
        return extra

    def expires_at(self) -> datetime | None:
        row = self._row()
        return None if row is None else row.expires_at

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        row = self._row()
        if row is None:
            return None
        return OAuthToken(
            access_token=row.access_token,
            token_type="Bearer",
            refresh_token=row.refresh_token,
            scope=row.scope or None,
        )

    async def set_tokens(self, tokens) -> None:
        expires_at = None
        if tokens.expires_in is not None:
            expires_at = datetime.now(UTC) + timedelta(seconds=int(tokens.expires_in))
        extra = self.stored_extra()
        self.store.save_connector_token(
            self.server,
            tokens.access_token,
            token_type=tokens.token_type,
            refresh_token=tokens.refresh_token,
            expires_at=expires_at,
            scope=tokens.scope or "",
            client_info=extra,
        )
        self.fresh = False
        self._pending = {}

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        data = self.stored_extra().get("client")
        return None if not data else OAuthClientInformationFull.model_validate(data)

    async def set_client_info(self, client_info) -> None:
        self.save_extra(client=client_info.model_dump(mode="json", exclude_none=True))

    def save_extra(self, **values: Any) -> None:
        """Merge values into client_info; kept in memory while no token row exists."""
        row = self._row()
        if row is None:
            self._pending.update(values)
            return
        extra = dict(row.client_info)
        extra.update(self._pending)
        extra.update(values)
        self._pending = {}
        self.store.save_connector_token(
            self.server,
            row.access_token,
            token_type=row.token_type,
            refresh_token=row.refresh_token,
            expires_at=row.expires_at,
            scope=row.scope,
            client_info=extra,
        )


def _provider_class():
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

    class HostOAuthProvider(OAuthClientProvider):
        """OAuthClientProvider that also saves and restores the server metadata
        and the token's expiry time."""

        storage_ref: StoreTokenStorage

        async def _initialize(self) -> None:
            await super()._initialize()
            storage = self.storage_ref
            extra = storage.stored_extra()
            if extra.get("auth_server"):
                self.context.oauth_metadata = OAuthMetadata.model_validate(extra["auth_server"])
            if extra.get("resource"):
                self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                    extra["resource"]
                )
                servers = [
                    str(u) for u in self.context.protected_resource_metadata.authorization_servers
                ]
                if servers:
                    self.context.auth_server_url = servers[0]
            expires_at = storage.expires_at()
            if self.context.current_tokens is not None and expires_at is not None:
                self.context.token_expiry_time = expires_at.timestamp()

        async def _save_metadata(self) -> None:
            values: dict[str, Any] = {}
            if self.context.oauth_metadata is not None:
                values["auth_server"] = self.context.oauth_metadata.model_dump(
                    mode="json", exclude_none=True
                )
            if self.context.protected_resource_metadata is not None:
                values["resource"] = self.context.protected_resource_metadata.model_dump(
                    mode="json", exclude_none=True
                )
            if values:
                self.storage_ref.save_extra(**values)

        async def _handle_token_response(self, response) -> None:
            await super()._handle_token_response(response)
            await self._save_metadata()

        async def _handle_refresh_response(self, response) -> bool:
            ok = await super()._handle_refresh_response(response)
            if ok:
                await self._save_metadata()
            return ok

    return HostOAuthProvider


def _client_metadata(redirect_uri: str, scope: str | None):
    from mcp.shared.auth import OAuthClientMetadata

    return OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
    )


def _make_provider(
    server: McpServerConfig,
    storage: StoreTokenStorage,
    redirect_uri: str,
    redirect_handler: Callable,
    callback_handler: Callable,
):
    cls = _provider_class()
    stored_client = storage.stored_extra().get("client") or {}
    uris = stored_client.get("redirect_uris") or [redirect_uri]
    provider = cls(
        server_url=server.url,
        client_metadata=_client_metadata(str(uris[0]), stored_client.get("scope")),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    provider.storage_ref = storage
    return provider


def gateway_provider(server: McpServerConfig, store: Store):
    """The auth for the gateway: stored token, refresh, never a browser."""

    async def no_redirect(url: str) -> None:
        raise SignInNeeded(_sign_in_message(server.name))

    async def no_callback():
        raise SignInNeeded(_sign_in_message(server.name))

    storage = StoreTokenStorage(store, server.name)
    return _make_provider(server, storage, "http://127.0.0.1/callback", no_redirect, no_callback)


def has_login(server: McpServerConfig, store: Store) -> bool:
    return store.get_connector_token(server.name) is not None


# ---------------------------------------------------------------------------
# Interactive sign-in
# ---------------------------------------------------------------------------


def parse_redirect(url: str) -> dict[str, str]:
    """code, state and iss from the address the browser was sent back to."""
    query = parse_qs(urlparse(url.strip()).query)
    if "error" in query:
        detail = query.get("error_description", query["error"])[0]
        raise LoginError(f"the server refused the sign-in: {detail}")
    if "code" not in query:
        raise LoginError("that address has no code= part; paste the whole address")
    return {k: v[0] for k, v in query.items() if k in ("code", "state", "iss")}


class _CallbackServer:
    """A one-shot HTTP listener on 127.0.0.1 for the browser's redirect."""

    def __init__(self, port: int):
        results: queue.Queue[dict[str, str] | Exception] = queue.Queue()
        self.results = results

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - name set by http.server
                if urlparse(self.path).path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                try:
                    results.put(parse_redirect(self.path))
                    body = b"Signed in. You can close this tab and go back to the terminal.\n"
                    status = 200
                except LoginError as exc:
                    results.put(exc)
                    body = f"Sign-in failed: {exc}\n".encode()
                    status = 400
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)
        self.redirect_uri = f"http://127.0.0.1:{self.httpd.server_address[1]}{CALLBACK_PATH}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _CallbackServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def wait(self, timeout: float) -> dict[str, str]:
        try:
            item = self.results.get(timeout=timeout)
        except queue.Empty as exc:
            raise LoginError("no answer from the browser in time") from exc
        if isinstance(item, Exception):
            raise item
        return item


def login(
    server: McpServerConfig,
    store: Store,
    *,
    open_browser: bool = True,
    paste: bool = False,
    port: int = 0,
    say: Callable[[str], None] = print,
    read_line: Callable[[str], str] = input,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> list[str]:
    """Sign in to one oauth connector and store the token. Returns the tool names
    the server lists, as a check that the new token works.

    paste: do not listen for the redirect; ask for the address the browser was
    sent to instead (for a host with no browser of its own).
    """
    import anyio
    from mcp.shared.auth import AuthorizationCodeResult

    if server.auth != "oauth":
        raise LoginError(f"connector {server.name} does not use oauth (auth = {server.auth!r})")
    if not server.url:
        raise LoginError(f"connector {server.name} has no url")

    with _CallbackServer(port) as callback:
        storage = StoreTokenStorage(store, server.name, fresh=True)

        async def redirect_handler(url: str) -> None:
            say("Open this address in a browser and sign in:")
            say(f"  {url}")
            if open_browser and not paste:
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001 - the address is printed anyway
                    pass

        async def callback_handler():
            if paste:
                text = await anyio.to_thread.run_sync(
                    read_line, "Paste the address the browser ended on: "
                )
                fields = parse_redirect(text)
            else:
                fields = await anyio.to_thread.run_sync(callback.wait, timeout)
            return AuthorizationCodeResult(**fields)

        provider = _make_provider(
            server, storage, callback.redirect_uri, redirect_handler, callback_handler
        )

        from opendot.gateway.upstream import list_remote_tools

        tools = anyio.run(list_remote_tools, server, provider)
        if storage.fresh:  # still true until a new token is saved
            raise LoginError("the server answered without asking for a sign-in; nothing was stored")
        return tools
