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


def _rates_file(days: int) -> Path:
    """期間別のキャッシュファイルパス."""
    return Path(f"data/sku_rates_{days}d.json")


def load_cached_rates(days: int | None = None) -> dict[str, float]:
    """キャッシュ済みのSKU別日次レートを返す.

    days=None  → 旧形式 data/sku_rates.json（後方互換）
    days=30/90 → data/sku_rates_30d.json / data/sku_rates_90d.json
    """
    if days is None:
        if RATES_FILE.exists():
            data = json.loads(RATES_FILE.read_text(encoding="utf-8"))
            return data.get("rates", {})
        return {}
    f = _rates_file(days)
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("rates", {})
    return {}


def save_rates(
    rates: dict[str, float],
    meta: dict[str, Any] | None = None,
    days: int | None = None,
) -> None:
    target = _rates_file(days) if days else RATES_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": datetime.now().isoformat(),
        "window_days": days,
        "sku_count": len(rates),
        "meta": meta or {},
        "rates": rates,
    }
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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


def fetch_amazon_sku_rates_multi(
    settings: Any,
    windows: list[int] | None = None,
) -> dict[int, dict[str, float]]:
    """Amazon SP-API から ASIN別に複数期間のレートを1度のAPI呼び出しで取得.

    最長期間分(max(windows)日)の日次データを1回取得し、
    各期間ごとに「期間内合計 ÷ 期間日数」で平均レートを算出する。

    Args:
        windows: 取得する日数のリスト（例 [7, 30, 90]）

    Returns:
        {window_days: {sku: daily_rate}}
    """
    from src.inventory import fetch_inventory
    from src.sp_client import SPClient
    from src.sale_calendar import SaleCalendar

    if not windows:
        windows = [7, 30, 90]
    windows = sorted(set(windows))
    longest = max(windows)

    calendar = SaleCalendar()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=longest)
    interval_start = start.strftime("%Y-%m-%dT00:00:00Z")
    interval_end = end.strftime("%Y-%m-%dT00:00:00Z")
    cutoffs = {w: (end - timedelta(days=w)).date() for w in windows}

    rates: dict[int, dict[str, float]] = {w: {} for w in windows}

    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
            asin_to_skus: dict[str, list[str]] = {}
            for item in items:
                if item.asin:
                    asin_to_skus.setdefault(item.asin, []).append(item.seller_sku)

            logger.info("[%s] %s日 マルチレート取得開始: ASIN %d件",
                        acc.name, "/".join(str(w) for w in windows), len(asin_to_skus))

            for i, (asin, skus) in enumerate(asin_to_skus.items()):
                try:
                    metrics = client.get_order_metrics(
                        interval_start, interval_end,
                        granularity="Day", asin=asin,
                    )
                    bucket: dict[int, list[int]] = {w: [] for w in windows}
                    for m in metrics:
                        s = m.get("interval", "").split("T")[0]
                        if not s:
                            continue
                        try:
                            d = date.fromisoformat(s)
                        except ValueError:
                            continue
                        if calendar.is_sale_day(d):
                            continue
                        units = m.get("unitCount", 0)
                        for w in windows:
                            if d >= cutoffs[w]:
                                bucket[w].append(units)

                    for w in windows:
                        rate = sum(bucket[w]) / w if bucket[w] else 0.0
                        for sku in skus:
                            rates[w][sku] = rate

                    if (i + 1) % 10 == 0:
                        logger.info("[%s] %d/%d 完了", acc.name, i + 1, len(asin_to_skus))
                    time.sleep(2.1)
                except Exception as e:
                    logger.warning("[%s] ASIN %s エラー: %s", acc.name, asin, e)
                    time.sleep(5)
        except Exception as e:
            logger.error("[%s] エラー: %s", acc.name, e)

    return rates


def fetch_amazon_sku_rates_dual(
    settings: Any,
    short_days: int = 30,
    long_days: int = 90,
) -> tuple[dict[str, float], dict[str, float]]:
    """Amazon SP-API から ASIN別に 1度のAPI呼び出しで 短期/長期 両方のレートを取得.

    long_days 期間の日次データを取得し、その中から短期分も計算する。
    セール日は除外し、期間全日数で割った平均（販売ゼロ日を含む）。

    Returns:
        (rates_short, rates_long) どちらも {sku: daily_rate}
    """
    from src.inventory import fetch_inventory
    from src.sp_client import SPClient
    from src.sale_calendar import SaleCalendar

    calendar = SaleCalendar()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=long_days)
    cutoff_short = (end - timedelta(days=short_days)).date()
    interval_start = start.strftime("%Y-%m-%dT00:00:00Z")
    interval_end = end.strftime("%Y-%m-%dT00:00:00Z")

    rates_short: dict[str, float] = {}
    rates_long: dict[str, float] = {}

    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
            asin_to_skus: dict[str, list[str]] = {}
            for item in items:
                if item.asin:
                    asin_to_skus.setdefault(item.asin, []).append(item.seller_sku)

            logger.info("[%s] %d/%d日レート取得開始: ASIN %d件",
                        acc.name, short_days, long_days, len(asin_to_skus))

            for i, (asin, skus) in enumerate(asin_to_skus.items()):
                try:
                    metrics = client.get_order_metrics(
                        interval_start, interval_end,
                        granularity="Day", asin=asin,
                    )
                    short_units = []
                    long_units = []
                    for m in metrics:
                        interval_str = m.get("interval", "").split("T")[0]
                        if not interval_str:
                            continue
                        try:
                            d = date.fromisoformat(interval_str)
                        except ValueError:
                            continue
                        if calendar.is_sale_day(d):
                            continue
                        units = m.get("unitCount", 0)
                        long_units.append(units)
                        if d >= cutoff_short:
                            short_units.append(units)

                    rate_short = sum(short_units) / short_days if short_units else 0.0
                    rate_long  = sum(long_units)  / long_days  if long_units  else 0.0

                    for sku in skus:
                        rates_short[sku] = rate_short
                        rates_long[sku]  = rate_long

                    if (i + 1) % 10 == 0:
                        logger.info("[%s] %d/%d 完了", acc.name, i + 1, len(asin_to_skus))
                    time.sleep(2.1)  # SP-API Sales: 0.5 req/s
                except Exception as e:
                    logger.warning("[%s] ASIN %s エラー: %s", acc.name, asin, e)
                    time.sleep(5)
        except Exception as e:
            logger.error("[%s] エラー: %s", acc.name, e)

    return rates_short, rates_long


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


def fetch_rakuten_sku_rates_multi(
    settings: Any,
    windows: list[int] | None = None,
) -> dict[int, dict[str, float]]:
    """楽天RMSから複数期間のSKUレートを1度の取得で算出.

    最長期間分の注文データを取得し、各期間で集計。

    Returns: {window_days: {sku: daily_rate}}
    """
    import base64
    from src.sale_calendar import SaleCalendar

    if not windows:
        windows = [7, 30, 90]
    windows = sorted(set(windows))
    longest = max(windows)

    calendar = SaleCalendar()
    rates: dict[int, dict[str, float]] = {w: {} for w in windows}

    SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/"
    GET_URL    = "https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/"

    for acc in settings.get_rakuten_accounts():
        try:
            credential = f"{acc.service_secret}:{acc.license_key}"
            encoded = base64.b64encode(credential.encode()).decode()
            headers = {
                "Authorization": f"ESA {encoded}",
                "Content-Type": "application/json; charset=utf-8",
            }

            end_dt = datetime.now()
            cutoffs = {w: (end_dt - timedelta(days=w)).date() for w in windows}

            # searchOrder で全期間の注文番号を 60日チャンクで取得
            CHUNK = 60
            all_order_nums: list[str] = []
            chunks_remaining = longest
            chunk_end = end_dt
            while chunks_remaining > 0:
                cd = min(CHUNK, chunks_remaining)
                chunk_start = chunk_end - timedelta(days=cd)
                page = 1
                while True:
                    payload = {
                        "dateType": 1,
                        "startDatetime": chunk_start.strftime("%Y-%m-%dT00:00:00+0900"),
                        "endDatetime":   chunk_end.strftime("%Y-%m-%dT23:59:59+0900"),
                        "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page},
                    }
                    resp = requests.post(SEARCH_URL, headers=headers, json=payload, timeout=30)
                    if resp.status_code != 200:
                        logger.warning("[楽天:%s] searchOrder失敗 (status=%d): %s",
                                       acc.name, resp.status_code, resp.text[:200])
                        break
                    data = resp.json()
                    nums = data.get("orderNumberList", []) or []
                    if not nums:
                        break
                    all_order_nums.extend(nums)
                    pag = data.get("PaginationResponseModel", {}) or {}
                    if page >= pag.get("totalPages", 1):
                        break
                    page += 1
                    time.sleep(2.1)
                chunks_remaining -= cd
                chunk_end = chunk_start

            all_order_nums = list(dict.fromkeys(all_order_nums))
            logger.info("[楽天:%s] 注文番号 %d件取得", acc.name, len(all_order_nums))

            # getOrder バッチで日別SKU集計
            sku_totals: dict[int, dict[str, int]] = {w: {} for w in windows}
            for i in range(0, len(all_order_nums), 100):
                batch = all_order_nums[i:i+100]
                payload = {"orderNumberList": batch, "version": 7}
                resp = requests.post(GET_URL, headers=headers, json=payload, timeout=60)
                if resp.status_code != 200:
                    logger.warning("[楽天:%s] getOrder失敗 (%d): %s",
                                   acc.name, resp.status_code, resp.text[:200])
                    time.sleep(5)
                    continue
                d = resp.json()
                for o in (d.get("OrderModelList") or []):
                    odate_str = (o.get("orderDatetime") or "")[:10]
                    if not odate_str:
                        continue
                    try:
                        odate = date.fromisoformat(odate_str)
                    except ValueError:
                        continue
                    if calendar.is_sale_day(odate):
                        continue
                    for pkg in (o.get("PackageModelList") or []):
                        for item in (pkg.get("ItemModelList") or []):
                            mn = item.get("manageNumber", "")
                            # variantId は item直下と SkuModelList の両方をチェック
                            # （楽天 getOrder では SkuModelList[].variantId に入る）
                            vid = item.get("variantId", "") or ""
                            if not vid:
                                sku_list = item.get("SkuModelList") or []
                                if sku_list:
                                    vid = (sku_list[0].get("variantId") or
                                           sku_list[0].get("merchantDefinedSkuId") or "")
                            sku = f"{mn}:{vid}" if vid else mn
                            qty = int(item.get("units", 0))
                            if not sku or qty <= 0:
                                continue
                            for w in windows:
                                if odate >= cutoffs[w]:
                                    sku_totals[w][sku] = sku_totals[w].get(sku, 0) + qty
                if (i // 100 + 1) % 5 == 0:
                    logger.info("[楽天:%s] getOrder %d/%d 完了",
                                acc.name, min(i+100, len(all_order_nums)), len(all_order_nums))
                time.sleep(2.1)

            for w in windows:
                for sku, total in sku_totals[w].items():
                    rates[w][sku] = total / w
            logger.info("[楽天:%s] %d SKU レート算出完了", acc.name,
                        max(len(sku_totals[w]) for w in windows))
        except Exception as e:
            logger.error("[楽天:%s] エラー: %s", acc.name, e)

    return rates


def fetch_rakuten_sku_rates_dual(
    settings: Any,
    short_days: int = 30,
    long_days: int = 90,
) -> tuple[dict[str, float], dict[str, float]]:
    """楽天RMS注文履歴から短期/長期 両方のレートを一度の取得で算出.

    フロー:
      1. searchOrder (POST) で対象期間の注文番号一覧を取得 (ページング対応)
      2. 100件ずつ getOrder (POST) で注文詳細(itemModelList)を取得
      3. 日次SKU別販売数を集計 → 短期/長期 平均レートに変換

    注意:
      - searchOrder は orderNumberList のみ返す。アイテム詳細は含まれない。
      - getOrder の MAX 件数は 100/リクエスト。
      - rateLimit: searchOrder 1req/2s, getOrder 1req/2s。
    """
    import base64
    from src.sale_calendar import SaleCalendar

    calendar = SaleCalendar()
    rates_short: dict[str, float] = {}
    rates_long: dict[str, float] = {}

    SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/"
    GET_URL    = "https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/"

    for acc in settings.get_rakuten_accounts():
        try:
            credential = f"{acc.service_secret}:{acc.license_key}"
            encoded = base64.b64encode(credential.encode()).decode()
            headers = {
                "Authorization": f"ESA {encoded}",
                "Content-Type": "application/json; charset=utf-8",
            }

            end_dt = datetime.now()
            cutoff_short = (end_dt - timedelta(days=short_days)).date()

            # 楽天 searchOrder は 1リクエストにつき最大63日。
            # long_days を 60日チャンクに分割して順次取得する。
            CHUNK_DAYS = 60
            all_order_nums: list[str] = []
            chunks_remaining = long_days
            chunk_end = end_dt
            while chunks_remaining > 0:
                cd = min(CHUNK_DAYS, chunks_remaining)
                chunk_start = chunk_end - timedelta(days=cd)
                page = 1
                while True:
                    payload = {
                        "dateType": 1,
                        "startDatetime": chunk_start.strftime("%Y-%m-%dT00:00:00+0900"),
                        "endDatetime":   chunk_end.strftime("%Y-%m-%dT23:59:59+0900"),
                        "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page},
                    }
                    resp = requests.post(SEARCH_URL, headers=headers, json=payload, timeout=30)
                    if resp.status_code != 200:
                        logger.warning("[楽天:%s] searchOrder失敗 (status=%d): %s",
                                       acc.name, resp.status_code, resp.text[:200])
                        break
                    data = resp.json()
                    nums = data.get("orderNumberList", []) or []
                    if not nums:
                        break
                    all_order_nums.extend(nums)
                    pag = data.get("PaginationResponseModel", {}) or {}
                    if page >= pag.get("totalPages", 1):
                        break
                    page += 1
                    time.sleep(2.1)
                chunks_remaining -= cd
                chunk_end = chunk_start

            # 重複除去（チャンク境界で重複の可能性は低いが念のため）
            all_order_nums = list(dict.fromkeys(all_order_nums))
            logger.info("[楽天:%s] 注文番号 %d件取得", acc.name, len(all_order_nums))

            # 2. getOrder でバッチ取得 (100件ずつ)
            sku_short_total: dict[str, int] = {}
            sku_long_total:  dict[str, int] = {}
            for i in range(0, len(all_order_nums), 100):
                batch = all_order_nums[i:i+100]
                payload = {"orderNumberList": batch, "version": 7}
                resp = requests.post(GET_URL, headers=headers, json=payload, timeout=60)
                if resp.status_code != 200:
                    logger.warning("[楽天:%s] getOrder失敗 (status=%d): %s", acc.name, resp.status_code, resp.text[:200])
                    time.sleep(5)
                    continue
                d = resp.json()
                orders = d.get("OrderModelList", []) or []
                for o in orders:
                    odate_str = (o.get("orderDatetime") or "")[:10]
                    if not odate_str:
                        continue
                    try:
                        odate = date.fromisoformat(odate_str)
                    except ValueError:
                        continue
                    if calendar.is_sale_day(odate):
                        continue
                    for pkg in (o.get("PackageModelList") or []):
                        for item in (pkg.get("ItemModelList") or []):
                            manage = item.get("manageNumber", "")
                            variant = item.get("variantId", "")
                            sku = f"{manage}:{variant}" if variant else manage
                            qty = int(item.get("units", 0))
                            if not sku or qty <= 0:
                                continue
                            sku_long_total[sku] = sku_long_total.get(sku, 0) + qty
                            if odate >= cutoff_short:
                                sku_short_total[sku] = sku_short_total.get(sku, 0) + qty
                if (i // 100 + 1) % 5 == 0:
                    logger.info("[楽天:%s] getOrder %d/%d 完了", acc.name, min(i+100, len(all_order_nums)), len(all_order_nums))
                time.sleep(2.1)

            for sku, total in sku_short_total.items():
                rates_short[sku] = total / short_days
            for sku, total in sku_long_total.items():
                rates_long[sku] = total / long_days

            logger.info("[楽天:%s] %d SKU レート算出完了", acc.name, len(sku_long_total))
        except Exception as e:
            logger.error("[楽天:%s] エラー: %s", acc.name, e)

    return rates_short, rates_long


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
