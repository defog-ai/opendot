"""The host's own MCP client connection to one connector.

A connector is either a streamable HTTP address (url) or a command the host runs
(command). The login is added here, on the host:

- auth = "bearer_env": the value of the host variable auth_env, sent as
  "Authorization: Bearer ..." to a url connector, or put in the command's
  environment under the same variable name;
- auth = "oauth": the token stored by `opendot connectors login` (url only);
- auth = "none": nothing.

Tests replace the whole connection with an UpstreamFactory.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from opendot import __version__

if TYPE_CHECKING:
    from opendot.config import McpServerConfig
    from opendot.store import Store

__all__ = [
    "UpstreamError",
    "UpstreamFactory",
    "list_all_tools",
    "list_remote_tools",
    "open_session",
]

# An async context manager that yields an initialized mcp ClientSession.
UpstreamFactory = Callable[["McpServerConfig"], AbstractAsyncContextManager[Any]]

CONNECT_TIMEOUT_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 300.0


class UpstreamError(RuntimeError):
    """The connector cannot be reached or its login is missing."""


def _client_info():
    from mcp import types

    return types.Implementation(name="opendot", version=__version__)


def _bearer(server: McpServerConfig, env: Mapping[str, str] | None) -> str:
    token = server.token(env)
    if not token:
        raise UpstreamError(
            f"connector {server.name}: set the host variable {server.auth_env} to its API key"
        )
    return token


@asynccontextmanager
async def open_session(
    server: McpServerConfig,
    *,
    store: Store | None = None,
    auth: Any = None,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[Any]:
    """An initialized ClientSession to server.

    auth: an httpx2.Auth to use instead of the configured login (sign-in uses it).
    store: needed for auth = "oauth"; it must belong to the calling thread.
    """
    from mcp.client.session import ClientSession

    if server.url:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        headers: dict[str, str] = {}
        if auth is None:
            if server.auth == "bearer_env":
                headers["Authorization"] = f"Bearer {_bearer(server, env)}"
            elif server.auth == "oauth":
                if store is None:
                    raise UpstreamError(f"connector {server.name}: no store for the oauth login")
                from opendot.gateway.oauth import SignInNeeded, gateway_provider, has_login

                if not has_login(server, store):
                    raise SignInNeeded(
                        f"connector {server.name} needs a sign-in: run "
                        f"`opendot connectors login {server.name}` on the host"
                    )
                auth = gateway_provider(server, store)
        timeout = httpx2.Timeout(CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS)
        async with httpx2.AsyncClient(headers=headers, auth=auth, timeout=timeout) as client:
            async with streamable_http_client(server.url, http_client=client) as (read, write):
                async with ClientSession(read, write, client_info=_client_info()) as session:
                    await session.initialize()
                    yield session
        return

    if not server.command:
        raise UpstreamError(f"connector {server.name} has neither url nor command")
    from mcp.client.stdio import StdioServerParameters, stdio_client

    child_env: dict[str, str] = {}
    if server.auth == "bearer_env":
        child_env[server.auth_env] = _bearer(server, env)
    params = StdioServerParameters(
        command=server.command[0], args=list(server.command[1:]), env=child_env
    )
    with open(os.devnull, "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, client_info=_client_info()) as session:
                await session.initialize()
                yield session


async def list_all_tools(session: Any) -> list[Any]:
    """Every tool the server lists, following pages."""
    from mcp import types

    tools: list[Any] = []
    cursor: str | None = None
    for _ in range(100):
        params = None if cursor is None else types.PaginatedRequestParams(cursor=cursor)
        result = await session.list_tools(params=params)
        tools.extend(result.tools)
        cursor = result.next_cursor
        if not cursor:
            break
    return tools


async def list_remote_tools(
    server: McpServerConfig,
    auth: Any = None,
    *,
    store: Store | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """The names of every tool the server lists."""
    async with open_session(server, store=store, auth=auth, env=env) as session:
        return [tool.name for tool in await list_all_tools(session)]
