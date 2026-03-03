from __future__ import annotations

import json
from pathlib import Path

from src.models import InventoryItem
from src.inventory import save_snapshot


def test_save_snapshot(tmp_path: Path):
    """スナップショット保存のテスト."""
    items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="テスト商品",
            fulfillable_quantity=50,
            total_quantity=60,
        )
    ]

    filepath = save_snapshot(items, tmp_path)
    assert filepath.exists()
    assert filepath.suffix == ".json"

    data = json.loads(filepath.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["seller_sku"] == "SKU-001"
    assert data[0]["fulfillable_quantity"] == 50
