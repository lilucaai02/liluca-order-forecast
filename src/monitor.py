from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from src.models import (
    AlertEvent,
    AlertLevel,
    InventoryItem,
    SalesVelocity,
    ThresholdConfig,
)


def load_thresholds(
    thresholds_file: Path,
) -> tuple[ThresholdConfig, dict[str, ThresholdConfig]]:
    """YAML閾値設定を読み込み. (defaults, {sku: config}) を返す."""
    with open(thresholds_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    defaults = ThresholdConfig(**(data.get("defaults", {})))
    sku_configs: dict[str, ThresholdConfig] = {}

    for sku, cfg in (data.get("skus") or {}).items():
        merged = defaults.model_dump()
        merged.update(cfg)
        sku_configs[sku] = ThresholdConfig(**merged)

    return defaults, sku_configs


def check_inventory(
    items: list[InventoryItem],
    defaults: ThresholdConfig,
    sku_configs: dict[str, ThresholdConfig],
    sales_data: dict[str, SalesVelocity] | None = None,
) -> list[AlertEvent]:
    """在庫レベルを閾値と比較し、アラートイベントを生成."""
    alerts: list[AlertEvent] = []

    for item in items:
        threshold = sku_configs.get(item.seller_sku, defaults)
        qty = item.fulfillable_quantity

        level: Optional[AlertLevel] = None
        if qty == 0:
            level = AlertLevel.OUT_OF_STOCK
        elif qty <= threshold.critical_level:
            level = AlertLevel.CRITICAL
        elif qty <= threshold.reorder_point:
            level = AlertLevel.WARNING

        if level is None:
            continue

        # 販売速度ベースの推定
        avg_daily: Optional[float] = None
        days_remaining: Optional[float] = None
        suggested_qty: Optional[int] = None

        velocity = None
        if sales_data:
            velocity = sales_data.get(item.seller_sku) or sales_data.get("ALL")
        if velocity and velocity.avg_daily_units > 0:
            avg_daily = velocity.avg_daily_units
            days_remaining = round(qty / avg_daily, 1)
            suggested_qty = max(
                0,
                int(
                    avg_daily
                    * threshold.lead_time_days
                    * threshold.safety_stock_multiplier
                )
                - qty,
            )

        # メッセージ生成
        if level == AlertLevel.OUT_OF_STOCK:
            msg = f"在庫切れ: {item.product_name} ({item.seller_sku})"
        elif level == AlertLevel.CRITICAL:
            msg = (
                f"在庫危険: {item.product_name} ({item.seller_sku}) "
                f"- 残{qty}個 (危険水準: {threshold.critical_level})"
            )
        else:
            msg = (
                f"在庫低下: {item.product_name} ({item.seller_sku}) "
                f"- 残{qty}個 (発注点: {threshold.reorder_point})"
            )

        if days_remaining is not None:
            msg += f" | 残約{days_remaining}日分"
        if suggested_qty:
            msg += f" | 推奨発注: {suggested_qty}個"

        alerts.append(
            AlertEvent(
                seller_sku=item.seller_sku,
                asin=item.asin,
                product_name=item.product_name,
                current_quantity=qty,
                reorder_point=threshold.reorder_point,
                level=level,
                message=msg,
                suggested_reorder_qty=suggested_qty,
                avg_daily_sales=avg_daily,
                estimated_days_of_stock=days_remaining,
            )
        )

    return alerts
