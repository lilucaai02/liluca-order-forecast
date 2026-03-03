from __future__ import annotations

from src.models import AlertLevel, InventoryItem, ThresholdConfig
from src.monitor import check_inventory


def test_no_alerts_when_stock_healthy(sample_items, default_threshold):
    """在庫が十分な場合はアラートなし."""
    healthy_items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="十分在庫",
            fulfillable_quantity=100,
            total_quantity=100,
        )
    ]
    alerts = check_inventory(healthy_items, default_threshold, {})
    assert len(alerts) == 0


def test_warning_alert(default_threshold):
    """発注点以下でWARNINGアラート."""
    items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="低在庫品",
            fulfillable_quantity=30,
            total_quantity=30,
        )
    ]
    alerts = check_inventory(items, default_threshold, {})
    assert len(alerts) == 1
    assert alerts[0].level == AlertLevel.WARNING
    assert alerts[0].current_quantity == 30


def test_critical_alert(default_threshold):
    """危険水準以下でCRITICALアラート."""
    items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="危険在庫品",
            fulfillable_quantity=5,
            total_quantity=5,
        )
    ]
    alerts = check_inventory(items, default_threshold, {})
    assert len(alerts) == 1
    assert alerts[0].level == AlertLevel.CRITICAL


def test_out_of_stock_alert(default_threshold):
    """在庫ゼロでOUT_OF_STOCKアラート."""
    items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="在庫切れ品",
            fulfillable_quantity=0,
            total_quantity=0,
        )
    ]
    alerts = check_inventory(items, default_threshold, {})
    assert len(alerts) == 1
    assert alerts[0].level == AlertLevel.OUT_OF_STOCK


def test_sku_specific_threshold():
    """SKU別の閾値が適用される."""
    defaults = ThresholdConfig(reorder_point=50, critical_level=10)
    sku_configs = {
        "SKU-001": ThresholdConfig(reorder_point=200, critical_level=50)
    }
    items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="カスタム閾値品",
            fulfillable_quantity=100,
            total_quantity=100,
        )
    ]
    alerts = check_inventory(items, defaults, sku_configs)
    assert len(alerts) == 1
    assert alerts[0].level == AlertLevel.WARNING


def test_mixed_inventory(sample_items, default_threshold):
    """複数SKUの混合テスト."""
    alerts = check_inventory(sample_items, default_threshold, {})
    # SKU-001: 100 > 50 -> 正常
    # SKU-002: 8 < 10 -> CRITICAL
    # SKU-003: 0 -> OUT_OF_STOCK
    # SKU-004: 40 < 50 -> WARNING
    assert len(alerts) == 3
    levels = {a.seller_sku: a.level for a in alerts}
    assert levels["SKU-002"] == AlertLevel.CRITICAL
    assert levels["SKU-003"] == AlertLevel.OUT_OF_STOCK
    assert levels["SKU-004"] == AlertLevel.WARNING


def test_sales_velocity_adds_suggestions(default_threshold, sample_sales_data):
    """販売速度データがある場合、推奨発注数が算出される."""
    items = [
        InventoryItem(
            asin="B000000001",
            seller_sku="SKU-001",
            product_name="低在庫品",
            fulfillable_quantity=30,
            total_quantity=30,
        )
    ]
    alerts = check_inventory(items, default_threshold, {}, sample_sales_data)
    assert len(alerts) == 1
    assert alerts[0].avg_daily_sales == 10.0
    assert alerts[0].estimated_days_of_stock == 3.0
    assert alerts[0].suggested_reorder_qty is not None
    assert alerts[0].suggested_reorder_qty > 0
