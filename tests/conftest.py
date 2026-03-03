from __future__ import annotations

import pytest

from src.models import InventoryItem, SalesVelocity, ThresholdConfig


@pytest.fixture
def sample_items() -> list[InventoryItem]:
    return [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="テスト商品A",
            fulfillable_quantity=100,
            total_quantity=120,
            reserved_quantity=10,
        ),
        InventoryItem(
            asin="B000000002",
            seller_sku="SKU-002",
            product_name="テスト商品B",
            fulfillable_quantity=8,
            total_quantity=8,
        ),
        InventoryItem(
            asin="B000000003",
            seller_sku="SKU-003",
            product_name="テスト商品C",
            fulfillable_quantity=0,
            total_quantity=0,
        ),
        InventoryItem(
            asin="B000000004",
            seller_sku="SKU-004",
            product_name="テスト商品D",
            fulfillable_quantity=40,
            total_quantity=45,
        ),
    ]


@pytest.fixture
def default_threshold() -> ThresholdConfig:
    return ThresholdConfig(
        reorder_point=50,
        critical_level=10,
        lead_time_days=14,
        safety_stock_multiplier=1.5,
    )


@pytest.fixture
def sample_sales_data() -> dict[str, SalesVelocity]:
    return {
        "ALL": SalesVelocity(
            seller_sku="ALL",
            asin="ALL",
            period_days=30,
            total_units_sold=300,
            avg_daily_units=10.0,
        )
    }
