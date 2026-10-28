"""On-demand Knuspr order retrieval via the community rohlik-api client.

This module intentionally has no scheduler and never writes inventory.
The user's explicit button press is the only way to invoke the API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rohlik_api import RohlikAPIError, InvalidCredentialsError


class KnusprOrderError(Exception):
    """A safe-to-display order retrieval error."""


def _sort_date(order: dict[str, Any]) -> str:
    return str(order.get("orderTime") or order.get("orderDate")
               or order.get("deliveryTime") or order.get("deliveredAt") or "")


async def fetch_orders(hass) -> list[dict[str, Any]]:
    """Reuse the active RohlikCZ integration session; no credential duplication."""
    accounts = [
        entry for entry in hass.config_entries.async_entries("rohlikcz")
        if entry.data.get("site") == "de" and getattr(entry, "runtime_data", None)
    ]
    if not accounts:
        raise KnusprOrderError(
            "Install HA-RohlikCZ, configure site Knuspr.de, then retry"
        )
    account = accounts[0].runtime_data
    client = getattr(account, "_client", None)
    if client is None:
        raise KnusprOrderError("Knuspr account session is not ready")
    try:
        listing = await client.orders.get_all_delivered()
        if isinstance(listing, dict):
            listing = listing.get("orders") or listing.get("data") or []
        if not isinstance(listing, list):
            raise KnusprOrderError("Knuspr returned an unrecognized order list")
        listing = [o for o in listing if isinstance(o, dict) and o.get("id") is not None]
        listing.sort(key=_sort_date, reverse=True)
        result = []
        for summary in listing[:3]:
            detail = await client.orders.get_detail(int(summary["id"]))
            if not isinstance(detail, dict) or not isinstance(detail.get("items"), list):
                raise KnusprOrderError("Knuspr returned invalid order items")
            rows = [{
                "id": i.get("id"), "name": i.get("name"),
                "quantity": i.get("amount", i.get("quantity", 1)),
                "unit": i.get("textualAmount", ""),
            } for i in detail["items"] if isinstance(i, dict)]
            result.append({
                "id": summary["id"], "orderTime": _sort_date(summary),
                "state": "delivered", "items": rows,
            })
        return result
    except (RohlikAPIError, InvalidCredentialsError) as err:
        raise KnusprOrderError("Knuspr order retrieval failed; check HA-RohlikCZ") from err
