"""Manual-only Knuspr MCP transport.

No periodic polling. OAuth token acquisition is deliberately NOT implemented:
Knuspr's generic-client registration has not yet been verified. A token may be
supplied by an authorized external OAuth client via the integration options.
Never log tokens or include them in websocket responses.
"""
from __future__ import annotations

import json
from typing import Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

MCP_URL = "https://mcp.knuspr.de/mcp"
RESOURCE_METADATA = "https://mcp.knuspr.de/.well-known/oauth-protected-resource"
AUTH_METADATA = "https://mcp.knuspr.de/.well-known/oauth-authorization-server"


class KnusprError(Exception):
    """Safe MCP transport error."""


async def discover(hass) -> dict[str, Any]:
    """Discover OAuth from the protected MCP resource, not guessed AS paths.

    RFC 9728: the resource may point to metadata via WWW-Authenticate.
    Authorization-server discovery must use the issuer advertised by that
    document (it might be on a different hostname).
    No credentials, orders, or other account data are requested.
    """
    import re
    from urllib.parse import urlparse

    session = async_get_clientsession(hass)
    result: dict[str, Any] = {}
    metadata_url = None

    # An unauthenticated MCP initialize elicits the authoritative challenge.
    try:
        async with session.post(
            MCP_URL,
            headers={"Accept": "application/json, text/event-stream",
                     "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26",
                             "capabilities": {},
                             "clientInfo": {"name": "fridge-assistant-discovery",
                                            "version": "0.7.3"}}},
            timeout=aiohttp.ClientTimeout(total=12),
            allow_redirects=False,
        ) as response:
            challenge = response.headers.get("WWW-Authenticate", "")
            result["mcp"] = {
                "status": response.status,
                "auth_scheme": challenge.split(" ", 1)[0] if challenge else None,
            }
            match = re.search(r'resource_metadata="([^"]+)"', challenge, re.I)
            if match:
                metadata_url = match.group(1)
    except (aiohttp.ClientError, TimeoutError) as err:
        result["mcp"] = {"error": type(err).__name__}

    # RFC 9728 alternative paths when the server omits the challenge URL.
    paths = [metadata_url] if metadata_url else [
        "https://mcp.knuspr.de/.well-known/oauth-protected-resource/mcp",
        "https://mcp.knuspr.de/.well-known/oauth-protected-resource",
    ]
    # Never follow arbitrary server-provided URLs to private hosts.
    def valid_https_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and bool(parsed.hostname) and (
            parsed.hostname == "knuspr.de" or parsed.hostname.endswith(".knuspr.de")
            or parsed.hostname == "rohlik.cz" or parsed.hostname.endswith(".rohlik.cz")
        ) and parsed.username is None and parsed.password is None

    auth_servers = []
    for url in paths:
        if not url or not valid_https_url(url):
            result["resource"] = {"error": "unsupported_metadata_host"}
            continue
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12),
                                   headers={"Accept": "application/json"},
                                   allow_redirects=False) as response:
                result["resource"] = {"status": response.status, "url": url}
                if response.status == 200 and response.content_type == "application/json":
                    doc = await response.json()
                    auth_servers = doc.get("authorization_servers", [])
                    result["resource"]["authorization_servers"] = auth_servers
                    if auth_servers:
                        break
        except (aiohttp.ClientError, TimeoutError) as err:
            result["resource"] = {"error": type(err).__name__, "url": url}

    # The AS metadata is located using the actual issuer URL, not the MCP
    # resource host. A 404 from one guessed path says nothing about OAuth.
    if auth_servers:
        issuer = auth_servers[0]
        if valid_https_url(issuer):
            parsed = urlparse(issuer)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            suffix = parsed.path.rstrip("/")
            candidates = [
                origin + "/.well-known/oauth-authorization-server" + suffix,
                origin + "/.well-known/openid-configuration" + suffix,
            ]
            for url in candidates:
                try:
                    async with session.get(url, headers={"Accept": "application/json"},
                                           timeout=aiohttp.ClientTimeout(total=12),
                                           allow_redirects=False) as response:
                        result["authorization"] = {"url": url, "status": response.status}
                        if response.status == 200 and response.content_type == "application/json":
                            doc = await response.json()
                            result["authorization"]["metadata"] = {
                                k: doc[k] for k in (
                                    "issuer", "authorization_endpoint", "token_endpoint",
                                    "registration_endpoint",
                                    "client_id_metadata_document_supported",
                                    "code_challenge_methods_supported",
                                ) if k in doc
                            }
                            break
                except (aiohttp.ClientError, TimeoutError) as err:
                    result["authorization"] = {"url": url, "error": type(err).__name__}
        else:
            result["authorization"] = {"error": "unsupported_authorization_host"}
    return result


def _decode_mcp_payload(body: str) -> dict[str, Any]:
    """Accept JSON response or an MCP event-stream payload."""
    try:
        return json.loads(body)
    except ValueError:
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                    if "result" in obj or "error" in obj:
                        return obj
                except ValueError:
                    pass
    raise KnusprError("MCP response was not valid JSON-RPC")


async def fetch_last_three(hass, access_token: str) -> list[dict[str, Any]]:
    """Make a user-triggered request only; never use a recurring timer."""
    if not access_token.strip():
        raise KnusprError("OAuth access token not configured")
    session = async_get_clientsession(hass)
    headers = {
        "Authorization": f"Bearer {access_token.strip()}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-03-26",
    }
    session_id = None
    async def rpc(method: str, params: dict[str, Any], request_id: int):
        nonlocal session_id
        h = dict(headers)
        if session_id:
            h["Mcp-Session-Id"] = session_id
        async with session.post(MCP_URL, headers=h, json={
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status == 401:
                raise KnusprError("Knuspr OAuth token expired or rejected")
            if response.status >= 400:
                raise KnusprError(f"Knuspr MCP HTTP {response.status}")
            session_id = response.headers.get("Mcp-Session-Id", session_id)
            data = _decode_mcp_payload(await response.text())
            if data.get("error"):
                raise KnusprError("Knuspr MCP returned a protocol error")
            return data.get("result") or {}
    await rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "fridge-assistant", "version": "0.7.2"},
    }, 1)
    # MCP requires initialized notification before tool calls.
    h = dict(headers)
    if session_id:
        h["Mcp-Session-Id"] = session_id
    async with session.post(MCP_URL, headers=h, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }, timeout=aiohttp.ClientTimeout(total=10)) as response:
        if response.status >= 400:
            raise KnusprError(f"MCP initialization failed: HTTP {response.status}")
    result = await rpc("tools/call", {
        "name": "fetch_orders",
        "arguments": {"limit": 3, "order_type": "delivered"},
    }, 2)
    if result.get("isError"):
        raise KnusprError("Knuspr fetch_orders failed")
    structured = result.get("structuredContent")
    if structured is None:
        for part in result.get("content", []):
            if part.get("type") == "text":
                try:
                    structured = json.loads(part["text"])
                    break
                except (KeyError, ValueError):
                    continue
    if isinstance(structured, dict):
        payload = structured.get("result", structured)
        if isinstance(payload, dict) and isinstance(payload.get("orders"), list):
            return payload["orders"][:3]
    raise KnusprError("Knuspr MCP did not return an orders list")
