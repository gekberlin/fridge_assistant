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
    """Probe public OAuth metadata (no account access or background traffic)."""
    session = async_get_clientsession(hass)
    result = {}
    for key, url in (("resource", RESOURCE_METADATA), ("authorization", AUTH_METADATA)):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12),
                                   headers={"Accept": "application/json"}) as response:
                result[key] = {"status": response.status}
                if response.status == 200 and "json" in response.content_type:
                    doc = await response.json()
                    # Publish only non-secret discovery fields.
                    result[key]["metadata"] = {k: doc.get(k) for k in (
                        "resource", "authorization_servers", "issuer",
                        "authorization_endpoint", "token_endpoint",
                        "registration_endpoint", "code_challenge_methods_supported",
                    ) if k in doc}
        except (aiohttp.ClientError, TimeoutError) as err:
            result[key] = {"error": type(err).__name__}
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
