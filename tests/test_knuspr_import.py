"""Pure data-layer tests; run in an environment with Home Assistant installed."""
from custom_components.fridge_assistant.knuspr_import import normalize_order, pending_rows


def test_order_preview_does_not_autocommit():
    rows = normalize_order({"id": 100, "items": [
        {"id": 10, "name": "Joghurt", "quantity": 2, "unit": "Stück"},
        {"id": 11, "name": "Reis", "quantity": 1},
    ]})
    assert len(rows) == 2
    assert rows[0]["location"] == "fridge"
    assert rows[1]["location"] == "pantry_kitchen"
    assert all(row["requires_review"] for row in rows)
    assert pending_rows(rows, {rows[0]["source_key"]}) == rows[1:]


def test_alternate_order_shape():
    rows = normalize_order({"id": 5, "items": {"enabledData": [
        {"inventoryId": 7, "name": "Tiefkühl Erbsen", "pieces": 3}
    ]}})
    assert rows[0]["location"] == "freezer"
    assert rows[0]["quantity"] == 3
