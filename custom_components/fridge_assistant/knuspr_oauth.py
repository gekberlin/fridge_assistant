"""Knuspr OAuth 2.1 / PKCE connection for manual-only MCP access."""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .const import DOMAIN
from .coordinator import get_runtime

AUTHORIZE = "https://identity.knuspr.de/oauth2/authorize"
TOKEN = "https://identity.knuspr.de/oauth2/token"
REGISTER = "https://identity.knuspr.de/connect/register"
CALLBACK_PATH = "/api/fridge_assistant/knuspr/oauth/callback"
PENDING_KEY = "knuspr_oauth_pending"
CLIENT_KEY = "knuspr_oauth_client"
TOKEN_KEY = "knuspr_oauth_token"
_LOGGER = logging.getLogger(__name__)


class OAuthConnectionError(Exception):
    """Non-sensitive, user-facing OAuth error."""


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _redirect_url(hass) -> str:
    # The callback must be reachable from the browser doing authorization.
    # Prefer the configured external HTTPS address; don't expose private
    # Home Assistant hostnames to the registration provider.
    try:
        base = get_url(hass, prefer_external=True, allow_internal=False)
    except Exception as err:
        raise OAuthConnectionError(
            "Configure an external HTTPS Home Assistant URL before connecting Knuspr"
        ) from err
    parsed = urlparse(base)
    if parsed.scheme != "https":
        raise OAuthConnectionError("Knuspr OAuth requires an HTTPS Home Assistant URL")
    return base.rstrip("/") + CALLBACK_PATH


async def start_login(hass) -> str:
    runtime = get_runtime(hass)
    if runtime is None:
        raise OAuthConnectionError("Fridge Assistant is not ready")
    redirect_uri = _redirect_url(hass)
    client = runtime.entry.options.get(CLIENT_KEY) or {}
    if client.get("redirect_uri") != redirect_uri or not client.get("client_id"):
        session = async_get_clientsession(hass)
        payload = {
            "client_name": "Fridge Assistant (Home Assistant)",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        try:
            async with session.post(REGISTER, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=20),
                                    allow_redirects=False) as response:
                if response.status not in (200, 201):
                    # RFC 7591 returns useful error/error_description fields.
                    # Never surface registration responses containing tokens.
                    reason = "unknown"
                    try:
                        error_body = await response.json(content_type=None)
                        if isinstance(error_body, dict):
                            code = str(error_body.get("error", "unknown"))
                            description = str(error_body.get("error_description", ""))
                            import re
                            # Keep only short non-sensitive diagnostic identifiers;
                            # do not reflect arbitrary provider text containing URLs.
                            if re.fullmatch(r"[a-zA-Z0-9_\\-]{1,70}", code):
                                reason = code
                            if description:
                                # Provider error descriptions help distinguish
                                # redirect, registration policy and payload errors.
                                # Redact URLs, email addresses and token-like
                                # strings before showing anything in the panel.
                                description = re.sub(r"https?://\\S+", "[URL]", description)
                                description = re.sub(
                                    r"\\b[^\\s@]+@[^\\s@]+\\.[^\\s@]+\\b",
                                    "[email]", description,
                                )
                                description = re.sub(
                                    r"\\b[A-Za-z0-9_-]{32,}\\b", "[redacted]",
                                    description,
                                )
                                description = re.sub(r"[\\r\\n\\t]+", " ", description)
                                reason += " — " + description[:180]
                    except (ValueError, aiohttp.ContentTypeError):
                        pass
                    _LOGGER.warning("Knuspr OAuth registration rejected: HTTP %s, reason: %s, redirect host: %s, grant: authorization_code, authentication: none", response.status, reason, urlparse(redirect_uri).hostname)
                    raise OAuthConnectionError(
                        f"Knuspr client registration HTTP {response.status}: {reason}"
                    )
                registered = await response.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OAuthConnectionError("Knuspr client registration unreachable") from err
        if not registered.get("client_id"):
            raise OAuthConnectionError("Knuspr registration did not return a client ID")
        client = {
            "client_id": registered["client_id"],
            "client_secret": registered.get("client_secret"),
            "redirect_uri": redirect_uri,
        }
        hass.config_entries.async_update_entry(
            runtime.entry, options={**runtime.entry.options, CLIENT_KEY: client}
        )
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    hass.data[DOMAIN][PENDING_KEY] = {
        "state": state, "verifier": verifier,
        "created": time.monotonic(), "client": client,
    }
    query = urlencode({
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": redirect_uri, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": "https://mcp.knuspr.de/mcp",
    })
    return AUTHORIZE + "?" + query


class KnusprOAuthCallback(HomeAssistantView):
    """Public callback protected by a random, short-lived, one-use state."""
    url = CALLBACK_PATH
    name = "api:fridge_assistant:knuspr_oauth_callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        pending = hass.data.get(DOMAIN, {}).pop(PENDING_KEY, None)
        if not pending or not secrets.compare_digest(
            request.query.get("state", ""), pending["state"]
        ) or time.monotonic() - pending["created"] > 300:
            return web.Response(text="Invalid or expired Knuspr OAuth state", status=400)
        if "error" in request.query:
            return web.Response(text="Knuspr authorization was declined", status=400)
        code = request.query.get("code", "")
        if not code:
            return web.Response(text="Missing authorization code", status=400)
        client = pending["client"]
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client["redirect_uri"],
            "client_id": client["client_id"],
            "code_verifier": pending["verifier"],
        }
        if client.get("client_secret"):
            data["client_secret"] = client["client_secret"]
        try:
            session = async_get_clientsession(hass)
            async with session.post(TOKEN, data=data,
                                    timeout=aiohttp.ClientTimeout(total=20),
                                    allow_redirects=False) as response:
                if response.status != 200:
                    return web.Response(text="Knuspr token exchange failed", status=400)
                token = await response.json()
        except (aiohttp.ClientError, TimeoutError):
            return web.Response(text="Knuspr token endpoint unavailable", status=502)
        if not token.get("access_token"):
            return web.Response(text="Knuspr did not return an access token", status=400)
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        runtime = get_runtime(hass)
        if runtime is None:
            return web.Response(text="Fridge Assistant is not ready", status=503)
        hass.config_entries.async_update_entry(
            runtime.entry, options={**runtime.entry.options, TOKEN_KEY: token}
        )
        return web.Response(
            text="<html><body><h2>Knuspr connected</h2><p>Return to Fridge Assistant.</p></body></html>",
            content_type="text/html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


async def access_token(hass) -> str:
    runtime = get_runtime(hass)
    if runtime is None:
        raise OAuthConnectionError("Fridge Assistant not ready")
    opts = runtime.entry.options
    token = dict(opts.get(TOKEN_KEY) or {})
    if not token.get("access_token"):
        raise OAuthConnectionError("Connect Knuspr first")
    if time.time() < float(token.get("expires_at", 0)) - 60:
        return token["access_token"]
    client = opts.get(CLIENT_KEY) or {}
    if not token.get("refresh_token"):
        raise OAuthConnectionError("Knuspr login expired: please reconnect")
    data = {
        "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
        "client_id": client["client_id"],
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    try:
        session = async_get_clientsession(hass)
        async with session.post(TOKEN, data=data,
                                timeout=aiohttp.ClientTimeout(total=20),
                                allow_redirects=False) as response:
            if response.status != 200:
                raise OAuthConnectionError("Knuspr token renewal failed: reconnect")
            refreshed = await response.json()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OAuthConnectionError("Knuspr token endpoint unavailable") from err
    if not refreshed.get("access_token"):
        raise OAuthConnectionError("Knuspr token renewal returned no access token")
    refreshed.setdefault("refresh_token", token["refresh_token"])
    refreshed["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600))
    hass.config_entries.async_update_entry(
        runtime.entry, options={**runtime.entry.options, TOKEN_KEY: refreshed}
    )
    return refreshed["access_token"]
