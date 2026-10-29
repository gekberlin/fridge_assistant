"""Interactive Knuspr OAuth via official Python MCP client (manual orders only).

Uses the proven loopback redirect URI from the user's standalone probe.
Callback URL is pasted into the HA panel; no local HTTP listener required.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

from homeassistant.helpers.storage import Store
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken,
)

SERVER = "https://mcp.knuspr.de/mcp"
REDIRECT = "http://localhost:8765/callback"
STORAGE_KEY = "fridge_assistant.knuspr_auth"


class KnusprSDKError(Exception):
    """Non-sensitive error for the frontend."""


class HAOAuthStorage(TokenStorage):
    def __init__(self, hass):
        self._store = Store(hass, 1, STORAGE_KEY)
        self._lock = asyncio.Lock()

    async def _read(self):
        return await self._store.async_load() or {}

    async def _update(self, key, value):
        async with self._lock:
            data = await self._read()
            data[key] = value
            await self._store.async_save(data)

    async def get_tokens(self):
        data = (await self._read()).get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens):
        await self._update("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self):
        data = (await self._read()).get("client")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client):
        await self._update("client", client.model_dump(mode="json"))

    async def reset(self):
        async with self._lock:
            await self._store.async_save({})


class KnusprBridge:
    def __init__(self, hass):
        self.hass = hass
        self.storage = HAOAuthStorage(hass)
        self._task = None
        self._url = None
        self._callback_future = None
        self._error = None
        self._ready = False
        self._lock = asyncio.Lock()

    def status(self):
        return {
            "login_url": self._url,
            "awaiting_callback": bool(self._callback_future and not self._callback_future.done()),
            "ready": self._ready,
            "error": self._error,
            "running": bool(self._task and not self._task.done()),
        }

    def _provider(self):
        metadata = OAuthClientMetadata(
            client_name="fridge-assistant",
            redirect_uris=[REDIRECT],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        return OAuthClientProvider(
            server_url=SERVER,
            client_metadata=metadata,
            storage=self.storage,
            redirect_handler=self._on_redirect,
            callback_handler=self._on_callback,
        )

    async def _on_redirect(self, url):
        self._url = url

    async def _on_callback(self):
        loop = asyncio.get_running_loop()
        self._callback_future = loop.create_future()
        try:
            return await asyncio.wait_for(self._callback_future, timeout=600)
        finally:
            self._callback_future = None

    async def start_login(self):
        if self._task and not self._task.done():
            return self.status()
        self._url = None
        self._error = None
        self._ready = False
        self._task = self.hass.async_create_background_task(self._login_worker(), "knuspr_oauth_login")
        return self.status()

    async def _login_worker(self):
        try:
            await self._connect(True)
        except KnusprSDKError:
            # _connect records a safe-to-display error; don't leave an
            # unhandled task exception in the event loop.
            pass

    async def submit_callback(self, url):
        future = self._callback_future
        if future is None or future.done():
            raise KnusprSDKError("No active OAuth authorization")
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != "localhost" or parsed.port != 8765 or parsed.path != "/callback":
            raise KnusprSDKError("Expected the localhost:8765/callback URL")
        query = parse_qs(parsed.query)
        if "error" in query:
            raise KnusprSDKError("Knuspr authorization denied")
        if not query.get("code") or not query.get("state"):
            raise KnusprSDKError("Callback URL lacks code or state")
        future.set_result(AuthorizationCodeResult(
            code=query["code"][0],
            state=query["state"][0],
            iss=query.get("iss", [None])[0],
        ))
        self._url = None
        return {"submitted": True}

    async def _connect(self, login_only=False):
        try:
            provider = self._provider()
            http_client = create_mcp_http_client(auth=provider)
            async with http_client:
                async with streamable_http_client(SERVER, http_client=http_client) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), timeout=660)
                        self._ready = True
                        self._error = None
                        if login_only:
                            return None
                        result = await session.call_tool(
                            "fetch_orders", {"limit": 3, "order_type": "delivered"}
                        )
                        if result.isError:
                            raise KnusprSDKError("Knuspr rejected order retrieval")
                        content = getattr(result, 'structured_content', None) or getattr(result, 'structuredContent', None)
                        if not content:
                            for part in result.content or []:
                                if getattr(part, "text", None):
                                    try:
                                        content = json.loads(part.text)
                                        break
                                    except ValueError:
                                        continue
                        payload = content.get("result", content) if isinstance(content, dict) else {}
                        orders = payload.get("orders") if isinstance(payload, dict) else None
                        if not isinstance(orders, list):
                            raise KnusprSDKError("Unexpected Knuspr order response")
                        return orders[:3]
        except KnusprSDKError as err:
            self._error = str(err)
            self._ready = False
            raise
        except Exception as err:
            self._error = type(err).__name__
            self._ready = False
            raise KnusprSDKError("Knuspr connection failed: " + type(err).__name__) from err

    async def fetch_orders(self):
        async with self._lock:
            if self._task and not self._task.done():
                raise KnusprSDKError("Login in progress")
            return await self._connect()

    async def logout(self):
        if self._task and not self._task.done():
            self._task.cancel()
        await self.storage.reset()
        self._url = None
        self._ready = False
        self._error = None


def get_bridge(hass):
    from .const import DOMAIN
    domain = hass.data.setdefault(DOMAIN, {})
    if "knuspr_bridge" not in domain:
        domain["knuspr_bridge"] = KnusprBridge(hass)
    return domain["knuspr_bridge"]
