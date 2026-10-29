"""Parse optional image URLs from Knuspr MCP product detail responses.

Never derive an image URL from a product ID. Only trust URLs returned by
the authenticated Knuspr MCP tool and only hostnames in Knuspr/Rohlik CDN.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse


def parse_mcp_result(result):
    payload = (getattr(result, "structured_content", None)
               or getattr(result, "structuredContent", None))
    if not payload:
        for part in getattr(result, "content", []) or []:
            text = getattr(part, "text", None)
            if text:
                try:
                    payload = json.loads(text)
                    break
                except (TypeError, ValueError):
                    continue
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload or {}


def _valid_url(raw):
    if not isinstance(raw, str):
        return ""
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme == "https" and
        (host == "knuspr.de" or host.endswith(".knuspr.de")
         or host == "rohlik.cz" or host.endswith(".rohlik.cz"))):
        return raw
    return ""


def _photo(obj, depth=0):
    if depth > 5:
        return ""
    if isinstance(obj, str):
        return _valid_url(obj)
    if isinstance(obj, list):
        for el in obj[:10]:
            found = _photo(el, depth + 1)
            if found:
                return found
    if isinstance(obj, dict):
        for key in ("imageUrl", "image_url", "image", "imageFrontUrl",
                    "mainImage", "primaryImage", "thumbnail", "images",
                    "imageUrls", "productImages", "photos", "picture"):
            if key in obj:
                found = _photo(obj[key], depth + 1)
                if found:
                    return found
        for key in ("url", "src", "original", "large", "medium", "small"):
            if key in obj:
                found = _photo(obj[key], depth + 1)
                if found:
                    return found
    return ""


def extract_photo_map(payload):
    """Map product IDs to real returned HTTPS image URLs, never guessed."""
    if isinstance(payload, dict):
        if payload.get("success") is False:
            return {}
        for key in ("products", "items", "results", "data", "productDetails"):
            if key in payload:
                return extract_photo_map(payload[key])
        if payload.get("id") or payload.get("productId"):
            payload = [payload]
        else:
            # Sometimes details are a map keyed by product ID.
            rows = []
            for key, item in payload.items():
                if str(key).isdigit() and isinstance(item, dict):
                    rows.append({"id": key, **item})
            payload = rows
    if not isinstance(payload, list):
        return {}
    result = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        product = row.get("product", row)
        if not isinstance(product, dict):
            continue
        product_id = product.get("productId") or product.get("id") or row.get("productId") or row.get("id")
        photo = _photo(product)
        if product_id is not None and photo:
            result[str(product_id)] = photo
    return result


async def fetch_direct_catalog_photos(hass, product_ids):
    """Lookup image URLs from Knuspr's *unofficial* public product-card API.

    See Rohlik API endpoint implementation: GET /api/v1/products/card,
    repeated products=ID and categoryType=normal. No Knuspr account
    credentials or MCP tokens are sent to this public endpoint.
    """
    import logging
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    logger = logging.getLogger(__name__)
    found = {}
    failures = 0
    stats = {"batches": 0, "http_200": 0, "cards": 0, "matched_ids": 0, "relative_images": 0, "response_type": "", "sample_fields": []}
    session = async_get_clientsession(hass)
    ids = list(dict.fromkeys(str(x) for x in product_ids if str(x).isdigit()))[:120]
    for start in range(0, len(ids), 15):
        batch = ids[start:start + 15]
        stats["batches"] += 1
        try:
            params = [("products", pid) for pid in batch]
            params.append(("categoryType", "normal"))
            async with session.get(
                "https://www.knuspr.de/api/v1/products/card",
                params=params,
                headers={"Accept": "application/json",
                         "User-Agent": "FridgeAssistant/0.9.8"},
                timeout=20,
            ) as response:
                if response.status != 200:
                    failures += 1
                    logger.warning("Knuspr product card API returned HTTP %s", response.status)
                    continue
                stats["http_200"] += 1
                payload = await response.json(content_type=None)
            # Payload shape may change across country/store deployments.
            entries = payload
            for _ in range(4):
                if not isinstance(entries, dict):
                    break
                wrapper = next((k for k in ("data", "products", "items", "result", "cards") if k in entries), None)
                if wrapper is None:
                    break
                entries = entries[wrapper]
            if isinstance(entries, dict):
                entries = list(entries.values())
            if not isinstance(entries, list):
                stats["response_type"] = type(entries).__name__
                stats["sample_fields"] = sorted(str(k) for k in entries.keys())[:12] if isinstance(entries, dict) else []
                failures += 1
                continue
            stats["cards"] += len(entries)
            if entries and isinstance(entries[0], dict):
                stats["sample_fields"] = sorted(str(k) for k in entries[0].keys())[:15]
            for item in entries:
                if not isinstance(item, dict):
                    continue
                pid = item.get("productId") or item.get("id")
                if str(pid) not in batch:
                    continue
                stats["matched_ids"] += 1
                raw = (item.get("imgPath") or item.get("imageUrl") or
                       item.get("image") or item.get("images"))
                url = _photo(raw)
                if not url and isinstance(raw, str) and raw.lstrip("/").startswith("images/"):
                    # The product-card API sometimes returns a CDN-relative
                    # image path. Prefix only paths supplied by that API.
                    stats["relative_images"] += 1
                    url = _valid_url("https://cdn.knuspr.de/" + raw.lstrip("/"))
                if url:
                    found[str(pid)] = url
        except Exception as err:
            failures += 1
            logger.warning("Knuspr product card API error: %s", type(err).__name__)
    logger.warning("Knuspr direct catalog photo diagnostics: %s found=%d failures=%d", stats, len(found), failures)
    return found, failures, stats
