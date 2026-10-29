"""Conservative on-demand Open Food Facts lookup for Knuspr order items.

OFF full-text search uses its legacy search endpoint. This module never
automatically assigns a match or modifies inventory.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientError, ClientResponseError
import re
import time
from difflib import SequenceMatcher

from homeassistant.helpers.aiohttp_client import async_get_clientsession

SEARCH_URL = "https://search.openfoodfacts.org/search"
FIELDS = "code,product_name,product_name_de,brands,image_front_small_url,image_front_url,image_url,quantity"
UA = "FridgeAssistant/0.9.0 (https://github.com/gekberlin/fridge_assistant/issues)"
_LOGGER = logging.getLogger(__name__)


class OpenFoodFactsError(Exception):
    """Safe-to-display lookup error, excluding URLs and query text."""
_MIN_INTERVAL = 6.5
_last_request = 0.0
_lock = asyncio.Lock()


def _clean(text):
    text = re.sub(r"\b(bio|demeter|stk|stück|glas|mehrweg)\b", " ", str(text).casefold())
    return re.sub(r"[^\w]+", " ", text).strip()


async def search_products(hass, name):
    global _last_request
    query = " ".join(str(name).split())[:120]
    if not query:
        return []
    async with _lock:
        delay = _MIN_INTERVAL - (time.monotonic() - _last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        _last_request = time.monotonic()
        session = async_get_clientsession(hass)
        try:
            async with session.get(
                SEARCH_URL,
            params={"q": re.sub(r"[^\\w\\s-]", " ", query), "page_size": "8", "langs": "de,en"},
                headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except ClientResponseError as err:
            _LOGGER.warning("Open Food Facts HTTP %s", err.status)
            raise OpenFoodFactsError(f"Open Food Facts HTTP {err.status}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.warning("Open Food Facts search timed out")
            raise OpenFoodFactsError("Zeitüberschreitung bei Open Food Facts") from err
        except (ClientError, ValueError) as err:
            _LOGGER.warning("Open Food Facts failed: %s", type(err).__name__)
            raise OpenFoodFactsError("Verbindungs-/Antwortfehler: " + type(err).__name__) from err
    results = []
    target = _clean(name)
    for hit in payload.get("hits", []):
        product = hit.get("_source", hit) if isinstance(hit, dict) else {}
        if not isinstance(product, dict):
            continue
        title = product.get("product_name_de") or product.get("product_name")
        barcode = str(product.get("code") or "")
        if not title or not barcode.isdigit():
            continue
        brand = str(product.get("brands") or "")
        score = SequenceMatcher(None, target, _clean(brand + " " + title)).ratio()
        score = max(score, SequenceMatcher(None, target, _clean(title)).ratio())
        photo = (product.get("image_front_small_url") or product.get("image_front_url")
                 or product.get("image_url") or "")
        if photo and (not photo.startswith("https://") or "openfoodfacts.org/" not in photo):
            photo = ""
        results.append({"name": title, "brands": brand, "barcode": barcode,
                        "photo": photo, "quantity": product.get("quantity") or "",
                        "score": round(score, 2)})
    return sorted(results, key=lambda item: item["score"], reverse=True)[:5]
