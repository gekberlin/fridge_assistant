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
