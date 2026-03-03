from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.models import InventoryItem
from src.sp_client import SPClient


def fetch_inventory(
    client: SPClient, skus: list[str] | None = None
) -> list[InventoryItem]:
    """SP-APIからFBA在庫を取得してInventoryItemリストに変換."""
    raw_summaries = client.get_inventory_summaries(seller_skus=skus)
    items: list[InventoryItem] = []

    for s in raw_summaries:
        detail = s.get("inventoryDetails", {})
        reserved = detail.get("reservedQuantity", {})
        unfulfillable = detail.get("unfulfillableQuantity", {})

        item = InventoryItem(
            asin=s.get("asin", ""),
            seller_sku=s.get("sellerSku", ""),
            product_name=s.get("productName", ""),
            condition=s.get("condition", "NewItem"),
            fulfillable_quantity=s.get("fulfillableQuantity", 0),
            inbound_shipped_quantity=detail.get("inboundShippedQuantity", 0),
            inbound_receiving_quantity=detail.get("inboundReceivingQuantity", 0),
            reserved_quantity=(
                reserved.get("totalReservedQuantity", 0)
                if isinstance(reserved, dict)
                else 0
            ),
            unfulfillable_quantity=(
                unfulfillable.get("totalUnfulfillableQuantity", 0)
                if isinstance(unfulfillable, dict)
                else 0
            ),
            total_quantity=s.get("totalQuantity", 0),
        )
        items.append(item)

    return items


def save_snapshot(items: list[InventoryItem], data_dir: Path) -> Path:
    """在庫スナップショットをJSONとして保存."""
    snapshot_dir = data_dir / "inventory_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filepath = snapshot_dir / f"snapshot_{timestamp}.json"

    data = [item.model_dump(mode="json") for item in items]
    filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return filepath
