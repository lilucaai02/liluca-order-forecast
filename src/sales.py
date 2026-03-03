from __future__ import annotations

from datetime import datetime, timedelta

from src.models import SalesVelocity
from src.sp_client import SPClient


def fetch_sales_velocity(
    client: SPClient,
    period_days: int = 30,
) -> dict[str, SalesVelocity]:
    """過去N日間の販売速度を取得. 全体集計を返す."""
    end = datetime.utcnow()
    start = end - timedelta(days=period_days)

    metrics = client.get_order_metrics(
        interval_start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        interval_end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        granularity="Day",
    )

    if not metrics:
        return {}

    total_units = sum(m.get("unitCount", 0) for m in metrics)
    avg_daily = total_units / max(period_days, 1)

    # getOrderMetricsはデフォルトでSKU別の内訳を返さないため、
    # 全体集計として返す。SKU別が必要な場合はReports APIを使う。
    velocity = SalesVelocity(
        seller_sku="ALL",
        asin="ALL",
        period_days=period_days,
        total_units_sold=int(total_units),
        avg_daily_units=round(avg_daily, 2),
    )
    return {"ALL": velocity}
