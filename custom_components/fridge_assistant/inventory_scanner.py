"""Scanner-independent inventory code resolution.

Both USB/Bluetooth keyboard-wedge readers and phone cameras can submit the
same decoded text to these helpers; frontend and service adapters are separate.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .codes import split_portion_code


def resolve_scan(code: str, items: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve internal sticker code first, then retail barcode.

    Retail barcodes may match multiple batches, so return all candidate ids
    and let the caller choose a batch before modifying inventory.
    """
    normalized = code.strip().upper()
    if not normalized:
        return None
    base, portion = split_portion_code(normalized)
    for item in items.values():
        if str(item.get("code") or "").upper() == base:
            return {"type": "sticker", "item_id": item["id"], "portion": portion}
    matches = [
        item["id"] for item in items.values()
        if str(item.get("barcode") or "").strip() == code.strip()
    ]
    if matches:
        return {"type": "retail", "item_ids": matches}
    return {"type": "unknown", "code": code.strip()}
