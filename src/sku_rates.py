"""SKU別日次消費量の高精度計算モジュール.

計算優先順位:
  1. 在庫スナップショット間の実消費量 (最高精度)
  2. SP-API / 楽天RMS の SKU別注文データ (高精度)
  3. アカウント合計を在庫比率で按分 (低精度フォールバック)

結果は data/sku_rates.json にキャッシュされる。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

RATES_FILE = Path("data/sku_rates.json")


def load_cached_rates() -> dict[str, float]:
    """キャッシュ済みのSKU別日次レートを返す."""
    if RATES_FILE.exists():
        data = json.loads(RATES_FILE.read_text(encoding="utf-8"))
        return data.get("rates", {})
    return {}


def save_rates(rates: dict[str, float], meta: dict[str, Any] | None = None) -> None:
    RATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": datetime.now().isoformat(),
        "sku_count": len(rates),
        "meta": meta or {},
        "rates": rates,
    }
    RATES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ─── Amazon ──────────────────────────────────────────────────────────────────

def fetch_amazon_sku_rates(
    settings: Any,
    days: int = 30,
    use_asin_api: bool = True,
) -> dict[str, float]:
    """Amazon SP-API から SKU 別日次消費量を取得.

    use_asin_api=True  → SKU ごとに getOrderMetrics(asin=...) を呼ぶ (精度高・時間かかる)
    use_asin_api=False → アカウント合計÷在庫按分 (精度低・即時)
    """
    from src.inventory import fetch_inventory
    from src.sp_client import SPClient
    from src.sale_calendar import SaleCalendar

    calendar = SaleCalendar()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    interval_start = start.strftime("%Y-%m-%dT00:00:00Z")
    interval_end = end.strftime("%Y-%m-%dT00:00:00Z")

    rates: dict[str, float] = {}

    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
            asin_to_skus: dict[str, list[str]] = {}
            for item in items:
                asin_to_skus.setdefault(item.asin, []).append(item.seller_sku)

            if use_asin_api:
                # ASIN ごとに API 呼び出し（高精度）
                logger.info("[%s] ASIN別レート取得開始: %d件", acc.name, len(asin_to_skus))
                for i, (asin, skus) in enumerate(asin_to_skus.items()):
                    if not asin:
                        continue
                    try:
                        metrics = client.get_order_metrics(
                            interval_start, interval_end,
                            granularity="Day", asin=asin,
                        )
                        # セール日を除いた通常日平均
                        normal_units = [
                            m.get("unitCount", 0)
                            for m in metrics
                            if m.get("interval", "").split("T")[0]
                            and not calendar.is_sale_day(
                                date.fromisoformat(m["interval"].split("T")[0])
                            )
                        ]
                        asin_rate = (
                            sum(normal_units) / len(normal_units) if normal_units else 0.0
                        )
                        for sku in skus:
                            rates[sku] = asin_rate
                        if (i + 1) % 10 == 0:
                            logger.info("[%s] %d/%d 完了", acc.name, i + 1, len(asin_to_skus))
                        # レート制限対策 (Sales API: 0.5 req/s)
                        time.sleep(2.1)
                    except Exception as e:
                        logger.warning("[%s] ASIN %s エラー: %s", acc.name, asin, e)
                        time.sleep(5)
            else:
                # フォールバック: アカウント合計÷在庫按分
                metrics = client.get_order_metrics(interval_start, interval_end, granularity="Day")
                normal_units = [
                    m.get("unitCount", 0) for m in metrics
                    if not calendar.is_sale_day(
                        date.fromisoformat(m.get("interval", "9999").split("T")[0])
                    )
                ]
                acc_rate = sum(normal_units) / len(normal_units) if normal_units else 0.0
                total_qty = sum(max(item.fulfillable_quantity, 0) for item in items)
                for item in items:
                    q = max(item.fulfillable_quantity, 0)
                    if total_qty > 0 and q > 0:
                        rates[item.seller_sku] = acc_rate * (q / total_qty)

        except Exception as e:
            logger.error("[%s] エラー: %s", acc.name, e)

    return rates


# ─── 楽天 ─────────────────────────────────────────────────────────────────────

def fetch_rakuten_sku_rates(settings: Any, days: int = 30) -> dict[str, float]:
    """楽天RMS の受注検索APIからSKU別日次消費量を取得."""
    import base64
    from src.sale_calendar import SaleCalendar

    calendar = SaleCalendar()
    rates: dict[str, float] = {}

    for acc in settings.get_rakuten_accounts():
        try:
            credential = f"{acc.service_secret}:{acc.license_key}"
            encoded = base64.b64encode(credential.encode()).decode()
            headers = {
                "Authorization": f"ESA {encoded}",
                "Content-Type": "application/json; charset=utf-8",
            }

            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=days)

            # 楽天RMS 注文検索API 2.0
            url = "https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder"
            payload = {
                "dateType": 1,  # 注文日
                "startDatetime": start_dt.strftime("%Y-%m-%dT00:00:00+0900"),
                "endDatetime": end_dt.strftime("%Y-%m-%dT23:59:59+0900"),
                "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": 1},
            }

            sku_daily: dict[str, list[int]] = {}  # sku -> per-day counts
            page = 1

            while True:
                payload["PaginationRequestModel"]["requestPage"] = page
                resp = requests.post(url, headers=headers, json=payload, timeout=30)

                if resp.status_code != 200:
                    logger.warning("[楽天:%s] 注文取得失敗: %s", acc.name, resp.status_code)
                    break

                data = resp.json()
                orders = data.get("orderModelList", []) or []
                if not orders:
                    break

                for order in orders:
                    order_date = (order.get("orderDatetime") or "")[:10]
                    if not order_date:
                        continue
                    try:
                        d = date.fromisoformat(order_date)
                    except ValueError:
                        continue
                    if calendar.is_sale_day(d):
                        continue  # セール日は除外

                    for item in order.get("packageModelList", [{}])[0].get(
                        "itemModelList", []
                    ):
                        manage_no = item.get("manageNumber", "")
                        variant_id = item.get("variantId", "")
                        sku = f"{manage_no}:{variant_id}" if variant_id else manage_no
                        qty = int(item.get("units", 0))
                        if sku:
                            sku_daily.setdefault(sku, []).append(qty)

                pagination = data.get("PaginationResponseModel", {})
                total_pages = pagination.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1
                time.sleep(0.5)

            # 日次平均を計算
            for sku, counts in sku_daily.items():
                rates[sku] = sum(counts) / days  # 期間全体の平均（販売ゼロ日を含む）

            logger.info("[楽天:%s] %d SKUのレート算出完了", acc.name, len(sku_daily))

        except Exception as e:
            logger.error("[楽天:%s] エラー: %s", acc.name, e)

    return rates


# ─── 統合更新 ─────────────────────────────────────────────────────────────────

def update_all_rates(settings: Any, use_asin_api: bool = True) -> dict[str, float]:
    """AmazonとRakutenのSKU別レートを一括更新してキャッシュに保存."""
    all_rates: dict[str, float] = {}

    print("Amazon SKUレートを取得中...")
    amazon_rates = fetch_amazon_sku_rates(settings, use_asin_api=use_asin_api)
    all_rates.update(amazon_rates)
    print(f"  → Amazon: {len(amazon_rates)} SKU")

    print("楽天SKUレートを取得中...")
    rakuten_rates = fetch_rakuten_sku_rates(settings)
    all_rates.update(rakuten_rates)
    print(f"  → 楽天: {len(rakuten_rates)} SKU")

    save_rates(all_rates, meta={"use_asin_api": use_asin_api})
    print(f"  → data/sku_rates.json 保存: {len(all_rates)} SKU合計")

    return all_rates


def build_thresholds_from_rates(
    rates: dict[str, float],
    lead_time_days: int = 14,
    safety_multiplier: float = 1.5,
    sale_buffer: float = 1.0,
    min_reorder: int = 3,
) -> dict[str, dict]:
    """レートから thresholds.yaml 用の設定辞書を生成."""
    skus_config = {}
    for sku, daily_rate in rates.items():
        if daily_rate <= 0:
            continue
        reorder = max(int(daily_rate * lead_time_days * safety_multiplier * sale_buffer), min_reorder)
        critical = max(int(reorder * 0.3), 1)
        skus_config[sku] = {
            "reorder_point": reorder,
            "critical_level": critical,
            "lead_time_days": lead_time_days,
            "safety_stock_multiplier": safety_multiplier,
        }
    return skus_config
