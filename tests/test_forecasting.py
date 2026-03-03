from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.forecasting import DemandForecaster, ForecastResult
from src.models import InventoryItem, SalesVelocity, ThresholdConfig


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def forecaster(data_dir: Path) -> DemandForecaster:
    return DemandForecaster(data_dir)


@pytest.fixture
def sample_item() -> InventoryItem:
    return InventoryItem(
        asin="B000000001",
        seller_sku="SKU-001",
        product_name="テスト商品",
        fulfillable_quantity=100,
        total_quantity=100,
    )


@pytest.fixture
def threshold() -> ThresholdConfig:
    return ThresholdConfig(
        reorder_point=50,
        critical_level=10,
        lead_time_days=14,
        safety_stock_multiplier=1.5,
    )


def _create_snapshots(data_dir: Path, sku: str, quantities: list, days_ago_start: int = 14):
    """テスト用のスナップショットを生成."""
    snapshot_dir = data_dir / "inventory_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    for i, qty in enumerate(quantities):
        ts = datetime.utcnow() - timedelta(days=days_ago_start - i)
        filename = f"snapshot_{ts.strftime('%Y%m%d_%H%M%S')}.json"
        data = [
            {
                "asin": "B000000001",
                "seller_sku": sku,
                "product_name": "テスト商品",
                "fulfillable_quantity": qty,
                "total_quantity": qty,
                "last_updated": ts.isoformat(),
            }
        ]
        (snapshot_dir / filename).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )


def test_load_empty_snapshots(forecaster):
    """スナップショットなしの場合は空DataFrame."""
    df = forecaster.load_snapshots()
    assert df.empty


def test_load_snapshots(data_dir, forecaster):
    """スナップショット読み込みテスト."""
    _create_snapshots(data_dir, "SKU-001", [100, 90, 80])
    df = forecaster.load_snapshots()
    assert len(df) == 3
    assert "snapshot_time" in df.columns


def test_consumption_rate(data_dir, forecaster):
    """消費率算出テスト: 14日間で100→30に減少 = 日次5個."""
    # 14日間で毎日5個ずつ減少
    quantities = [100 - (i * 5) for i in range(15)]  # 100, 95, ..., 30
    _create_snapshots(data_dir, "SKU-001", quantities, days_ago_start=14)

    df = forecaster.load_snapshots()
    rate = forecaster.compute_consumption_rate(df, "SKU-001", window_days=30)
    assert rate is not None
    assert 4.0 <= rate <= 6.0  # 約5個/日


def test_consumption_rate_no_data(forecaster):
    """データなしの場合はNone."""
    import pandas as pd
    rate = forecaster.compute_consumption_rate(pd.DataFrame(), "SKU-001")
    assert rate is None


def test_forecast_with_sales_velocity(forecaster, sample_item, threshold):
    """販売APIデータによる予測."""
    velocity = SalesVelocity(
        seller_sku="SKU-001",
        asin="B000000001",
        period_days=30,
        total_units_sold=300,
        avg_daily_units=10.0,
    )

    result = forecaster.forecast_sku(
        item=sample_item,
        threshold=threshold,
        sales_velocity=velocity,
    )

    assert result.daily_consumption_rate == 10.0
    assert result.days_until_stockout == 10.0  # 100 / 10
    assert result.recommended_order_qty > 0


def test_forecast_no_data(forecaster, sample_item, threshold):
    """データなしの場合はデータ不足として返す."""
    result = forecaster.forecast_sku(
        item=sample_item,
        threshold=threshold,
    )

    assert result.daily_consumption_rate == 0
    assert result.days_until_stockout is None
    assert result.recommended_order_qty == 0
    assert result.method == "データ不足"


def test_forecast_all(data_dir, threshold):
    """全SKU一括予測テスト."""
    items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="商品A",
            fulfillable_quantity=100,
            total_quantity=100,
        ),
        InventoryItem(
            asin="B000000002",
            seller_sku="SKU-002",
            product_name="商品B",
            fulfillable_quantity=20,
            total_quantity=20,
        ),
    ]

    sales_data = {
        "ALL": SalesVelocity(
            seller_sku="ALL",
            asin="ALL",
            period_days=30,
            total_units_sold=150,
            avg_daily_units=5.0,
        )
    }

    forecaster = DemandForecaster(data_dir)
    results = forecaster.forecast_all(items, threshold, {}, sales_data)

    assert len(results) == 2
    assert all(isinstance(r, ForecastResult) for r in results)
    # 両方とも販売APIのデータを使用
    assert results[0].daily_consumption_rate == 5.0
    assert results[1].daily_consumption_rate == 5.0
