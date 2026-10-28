"""On-demand Knuspr order retrieval via the community rohlik-api client.

This module intentionally has no scheduler and never writes inventory.
The user's explicit button press is the only way to invoke the API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rohlik_api import RohlikAPI, RohlikAPIError, InvalidCredentialsError, SITES


class KnusprOrderError(Exception):
    """A safe-to-display order retrieval error."""


def _sort_date(order: dict[str, Any]) -> str:
    return str(order.get("orderTime") or order.get("orderDate")
               or order.get("deliveryTime") or order.get("deliveredAt") or "")


async def fetch_orders(email: str, password: str) -> list[dict[str, Any]]:
    """Fetch most recent three delivered orders plus their items, only on demand."""
    client = RohlikAPI(email, password, base_url=SITES["de"].base_url)
    try:
        await client.login()
        listing = await client.orders.get_all_delivered()
        # rohlik-api normally returns a list; tolerate a wrapped result.
        if isinstance(listing, dict):
            listing = listing.get("orders") or listing.get("data") or []
        if not isinstance(listing, list):
            raise KnusprOrderError("Knuspr returned an unrecognized order list")
        listing = [o for o in listing if isinstance(o, dict) and o.get("id") is not None]
        listing.sort(key=_sort_date, reverse=True)
        result = []
        for summary in listing[:3]:
            order_id = summary["id"]
            detail = await client.orders.get_detail(int(order_id))
            if not isinstance(detail, dict):
                raise KnusprOrderError("Knuspr returned an unrecognized order detail")
            raw_items = detail.get("items") or []
            if not isinstance(raw_items, list):
                raise KnusprOrderError("Knuspr returned an unrecognized item list")
            rows = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                rows.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "quantity": item.get("amount", item.get("quantity", 1)),
                    "unit": item.get("textualAmount", ""),
                })
            result.append({
                "id": order_id,
                "orderTime": _sort_date(summary),
                "state": "delivered",
                "items": rows,
            })
        return result
    except InvalidCredentialsError as err:
        raise KnusprOrderError("Knuspr login rejected; check your credentials") from err
    except RohlikAPIError as err:
        raise KnusprOrderError("Knuspr order request failed; check Home Assistant logs") from err
    finally:
        await client.close()
