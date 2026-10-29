"""Prepare Knuspr order payloads for review before writing inventory.

This module deliberately has no Home Assistant or MCP client dependencies.
The caller must retrieve orders through an authenticated connector and pass
their data in; never store credentials in the integration.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import LOCATION_FRIDGE, LOCATION_FREEZER, LOCATION_PANTRY_KITCHEN

_FROZEN = ("tiefkühl", "tiefgefroren", "tk ", "frozen", "mražen")
_CHILLED = ("gekühlt", "frischmilch", "joghurt", "quark", "sahne", "käse", "butter",
            "frischfleisch", "frischer fisch", "kühlpflichtig")


def suggest_location(name: str, *, category: str = "", storage_hint: str = "") -> str | None:
    """Conservative suggestion. Unknown products require explicit user review."""
    description = " ".join((name, category, storage_hint)).casefold()
    if any(word in description for word in _FROZEN):
        return LOCATION_FREEZER
    if any(word in description for word in _CHILLED):
        return LOCATION_FRIDGE
    if any(word in description for word in ("konserve", "nudeln", "reis", "mehl", "fond", "trocken")):
        return LOCATION_PANTRY_KITCHEN
    return None


def normalize_order(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt the observed Knuspr order.items[] shape and alternate warranty shape.

    Each row uses the immutable order + position key to permit idempotent imports.
    The result is a preview, not an authorization to modify inventory.
    """
    order_id = order.get("id")
    if order_id is None:
        raise ValueError("Knuspr order has no id")
    raw = order.get("items") or []
    if isinstance(raw, Mapping):
        raw = [*(raw.get("enabledData") or []), *(raw.get("disabledData") or [])]
    elif not isinstance(raw, list):
        raise ValueError("Unsupported Knuspr order items format")
    result = []
    for position, product in enumerate(raw):
        if not isinstance(product, Mapping):
            continue
        name = str(product.get("name") or "").strip()
        if not name:
            continue
        quantity = product.get("quantity", product.get("pieces", 1))
        sku = product.get("id", product.get("inventoryId"))
        result.append({
            "source": "knuspr",
            "source_key": f"knuspr:{order_id}:{position}",
            "order_id": str(order_id),
            "product_id": str(sku) if sku is not None else None,
            "name": name,
            "photo": product.get("photo") or product.get("imageUrl") or "",
            "photo_source": product.get("photo_source") or ("knuspr" if product.get("photo") else ""),
            "quantity": quantity,
            "unit": product.get("unit") or product.get("textualAmount"),
            "location": suggest_location(name),
            "requires_review": True,
        })
    return result


def pending_rows(rows: list[dict[str, Any]], imported_keys: set[str]) -> list[dict[str, Any]]:
    """Return rows not already committed by the import coordinator."""
    return [row for row in rows if row["source_key"] not in imported_keys]
