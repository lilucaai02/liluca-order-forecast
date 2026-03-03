from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from src.models import InventoryItem, SalesVelocity, ThresholdConfig


class DemandForecaster:
    """過去の在庫スナップショットと販売データから需要を予測し、発注量を算出."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._snapshot_dir = data_dir / "inventory_snapshots"

    def load_snapshots(self) -> pd.DataFrame:
        """保存済みスナップショットをDataFrameに読み込み."""
        if not self._snapshot_dir.exists():
            return pd.DataFrame()

        records = []
        for f in sorted(self._snapshot_dir.glob("snapshot_*.json")):
            try:
                items = json.loads(f.read_text(encoding="utf-8"))
                for item in items:
                    item["snapshot_file"] = f.name
                    # ファイル名からタイムスタンプを抽出
                    ts_str = f.stem.replace("snapshot_", "")
                    item["snapshot_time"] = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                    records.append(item)
            except (json.JSONDecodeError, ValueError):
                continue

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df["snapshot_time"] = pd.to_datetime(df["snapshot_time"])
        return df

    def compute_consumption_rate(
        self,
        df: pd.DataFrame,
        seller_sku: str,
        window_days: int = 14,
    ) -> Optional[float]:
        """スナップショット間の在庫減少量から日次消費率を算出.

        Returns:
            1日あたりの平均消費量。データ不足の場合はNone。
        """
        if df.empty:
            return None

        sku_df = df[df["seller_sku"] == seller_sku].sort_values("snapshot_time")
        if len(sku_df) < 2:
            return None

        # 直近window_days分のデータに限定
        cutoff = datetime.utcnow() - timedelta(days=window_days)
        sku_df = sku_df[sku_df["snapshot_time"] >= cutoff]
        if len(sku_df) < 2:
            return None

        # 在庫の減少幅を時間で割る (入庫を考慮: 在庫が増えた区間はスキップ)
        total_consumed = 0.0
        total_hours = 0.0

        for i in range(1, len(sku_df)):
            prev = sku_df.iloc[i - 1]
            curr = sku_df.iloc[i]
            qty_diff = prev["fulfillable_quantity"] - curr["fulfillable_quantity"]
            hours = (curr["snapshot_time"] - prev["snapshot_time"]).total_seconds() / 3600

            if hours <= 0:
                continue

            # 在庫が減少した区間のみ消費とみなす
            if qty_diff > 0:
                total_consumed += qty_diff
                total_hours += hours

        if total_hours == 0:
            return 0.0

        daily_rate = (total_consumed / total_hours) * 24
        return round(daily_rate, 2)

    def forecast_sku(
        self,
        item: InventoryItem,
        threshold: ThresholdConfig,
        sales_velocity: Optional[SalesVelocity] = None,
        snapshot_df: Optional[pd.DataFrame] = None,
    ) -> ForecastResult:
        """単一SKUの需要予測と発注推奨を算出."""
        # 消費率の決定: スナップショットベース > 販売APIベース
        daily_rate = None

        if snapshot_df is not None and not snapshot_df.empty:
            daily_rate = self.compute_consumption_rate(snapshot_df, item.seller_sku)

        if daily_rate is None and sales_velocity is not None:
            daily_rate = sales_velocity.avg_daily_units

        if daily_rate is None or daily_rate <= 0:
            return ForecastResult(
                seller_sku=item.seller_sku,
                asin=item.asin,
                product_name=item.product_name,
                current_stock=item.fulfillable_quantity,
                daily_consumption_rate=0,
                days_until_stockout=None,
                days_until_reorder_point=None,
                recommended_order_qty=0,
                recommended_order_date=None,
                method="データ不足",
            )

        current = item.fulfillable_quantity
        lead_days = threshold.lead_time_days
        safety_multi = threshold.safety_stock_multiplier

        # 在庫切れまでの日数
        days_to_stockout = round(current / daily_rate, 1) if daily_rate > 0 else None

        # 発注点到達までの日数
        days_to_reorder = None
        if current > threshold.reorder_point and daily_rate > 0:
            days_to_reorder = round(
                (current - threshold.reorder_point) / daily_rate, 1
            )

        # 推奨発注量 = (リードタイム × 日次消費 × 安全在庫係数) - 現在庫
        target_stock = daily_rate * lead_days * safety_multi
        recommended_qty = max(0, int(target_stock - current))

        # 推奨発注日 = 在庫切れ日 - リードタイム
        recommended_date = None
        if days_to_stockout is not None:
            order_by_days = max(0, days_to_stockout - lead_days)
            if order_by_days <= lead_days:
                recommended_date = (
                    datetime.utcnow() + timedelta(days=order_by_days)
                ).strftime("%Y-%m-%d")

        method = "スナップショット分析" if snapshot_df is not None else "販売API"

        return ForecastResult(
            seller_sku=item.seller_sku,
            asin=item.asin,
            product_name=item.product_name,
            current_stock=current,
            daily_consumption_rate=daily_rate,
            days_until_stockout=days_to_stockout,
            days_until_reorder_point=days_to_reorder,
            recommended_order_qty=recommended_qty,
            recommended_order_date=recommended_date,
            method=method,
        )

    def forecast_all(
        self,
        items: list[InventoryItem],
        defaults: ThresholdConfig,
        sku_configs: dict[str, ThresholdConfig],
        sales_data: Optional[dict[str, SalesVelocity]] = None,
    ) -> list[ForecastResult]:
        """全SKUの需要予測を一括実行."""
        snapshot_df = self.load_snapshots()
        results = []

        for item in items:
            threshold = sku_configs.get(item.seller_sku, defaults)
            velocity = None
            if sales_data:
                velocity = sales_data.get(item.seller_sku) or sales_data.get("ALL")

            result = self.forecast_sku(
                item=item,
                threshold=threshold,
                sales_velocity=velocity,
                snapshot_df=snapshot_df if not snapshot_df.empty else None,
            )
            results.append(result)

        return results


class ForecastResult:
    """需要予測の結果."""

    def __init__(
        self,
        seller_sku: str,
        asin: str,
        product_name: str,
        current_stock: int,
        daily_consumption_rate: float,
        days_until_stockout: Optional[float],
        days_until_reorder_point: Optional[float],
        recommended_order_qty: int,
        recommended_order_date: Optional[str],
        method: str,
    ) -> None:
        self.seller_sku = seller_sku
        self.asin = asin
        self.product_name = product_name
        self.current_stock = current_stock
        self.daily_consumption_rate = daily_consumption_rate
        self.days_until_stockout = days_until_stockout
        self.days_until_reorder_point = days_until_reorder_point
        self.recommended_order_qty = recommended_order_qty
        self.recommended_order_date = recommended_order_date
        self.method = method
