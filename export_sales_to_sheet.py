#!/usr/bin/env python3
"""
販売実績を Google スプレッドシートに書き出すスクリプト

Amazon (SP-API) + 楽天 (RMS Order API) から過去N日の日別販売数を取得し、
新規 Google スプレッドシートに「SKU × 日付」のクロス集計表として書き込む。

シートに共有: 引数 --share user@example.com で指定（複数可、カンマ区切り）

使い方:
  python3 export_sales_to_sheet.py                              # 過去90日
  python3 export_sales_to_sheet.py --days 30                    # 過去30日
  python3 export_sales_to_sheet.py --share lilucaai02@gmail.com # 共有先指定
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import Settings
from src.inventory import fetch_inventory
from src.sp_client import SPClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


# ─── Amazon: ASIN別×日別販売数を取得 ────────────────────────────────────────
def fetch_amazon_daily(settings: Settings, days: int) -> tuple[dict, dict]:
    """Amazon SP-APIから ASIN×日別 販売数を取得.

    Returns:
        (daily_by_sku, sku_to_name)
        daily_by_sku: {sku: {date_str: units}}
        sku_to_name:  {sku: product_name}
    """
    daily_by_sku: dict = {}
    sku_to_name: dict = {}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    interval_start = start.strftime("%Y-%m-%dT00:00:00Z")
    interval_end = end.strftime("%Y-%m-%dT00:00:00Z")

    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
            asin_to_skus: dict = {}
            for x in items:
                if x.asin:
                    asin_to_skus.setdefault(x.asin, []).append(x.seller_sku)
                if x.product_name:
                    sku_to_name[x.seller_sku] = x.product_name[:50]

            logger.info("[Amazon:%s] ASIN %d件の日別データ取得開始", acc.name, len(asin_to_skus))
            for i, (asin, skus) in enumerate(asin_to_skus.items()):
                try:
                    metrics = client.get_order_metrics(interval_start, interval_end,
                                                        granularity="Day", asin=asin)
                    asin_daily: dict = {}
                    for m in metrics:
                        d = m.get("interval", "").split("T")[0]
                        if d:
                            asin_daily[d] = asin_daily.get(d, 0) + m.get("unitCount", 0)
                    # 同ASIN内の各SKUに同じ日別データを格納（後でユーザーが識別）
                    for sku in skus:
                        if sku not in daily_by_sku:
                            daily_by_sku[sku] = {}
                        for d, u in asin_daily.items():
                            daily_by_sku[sku][d] = max(daily_by_sku[sku].get(d, 0), u)
                    if (i + 1) % 10 == 0:
                        logger.info("[Amazon:%s] %d/%d 完了", acc.name, i + 1, len(asin_to_skus))
                    time.sleep(2.1)
                except Exception as e:
                    logger.warning("[Amazon:%s] ASIN %s エラー: %s", acc.name, asin, e)
                    time.sleep(5)
        except Exception as e:
            logger.error("[Amazon:%s] 全体エラー: %s", acc.name, e)

    return daily_by_sku, sku_to_name


# ─── 楽天: searchOrder + getOrder で日別×SKU 集計 ──────────────────────────
def fetch_rakuten_daily(settings: Settings, days: int) -> dict:
    """楽天RMSから日別×SKU 販売数を取得.

    Returns: {sku: {date_str: units}}
    """
    daily_by_sku: dict = {}

    SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/"
    GET_URL    = "https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/"

    for acc in settings.get_rakuten_accounts():
        try:
            cred = f"{acc.service_secret}:{acc.license_key}"
            headers = {
                "Authorization": f"ESA {base64.b64encode(cred.encode()).decode()}",
                "Content-Type": "application/json; charset=utf-8",
            }

            end_dt = datetime.now()
            CHUNK = 60
            order_nums: list = []
            chunks_remaining = days
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
                    r = requests.post(SEARCH_URL, headers=headers, json=payload, timeout=30)
                    if r.status_code != 200:
                        logger.warning("[楽天:%s] searchOrder %d: %s", acc.name, r.status_code, r.text[:200])
                        break
                    d = r.json()
                    nums = d.get("orderNumberList", []) or []
                    if not nums:
                        break
                    order_nums.extend(nums)
                    pag = d.get("PaginationResponseModel", {}) or {}
                    if page >= pag.get("totalPages", 1):
                        break
                    page += 1
                    time.sleep(2.1)
                chunks_remaining -= cd
                chunk_end = chunk_start

            order_nums = list(dict.fromkeys(order_nums))
            logger.info("[楽天:%s] 注文番号 %d件取得", acc.name, len(order_nums))

            for i in range(0, len(order_nums), 100):
                batch = order_nums[i:i+100]
                payload = {"orderNumberList": batch, "version": 7}
                r = requests.post(GET_URL, headers=headers, json=payload, timeout=60)
                if r.status_code != 200:
                    logger.warning("[楽天:%s] getOrder %d: %s", acc.name, r.status_code, r.text[:200])
                    time.sleep(5)
                    continue
                d = r.json()
                for o in (d.get("OrderModelList") or []):
                    odate_str = (o.get("orderDatetime") or "")[:10]
                    if not odate_str:
                        continue
                    for pkg in (o.get("PackageModelList") or []):
                        for it in (pkg.get("ItemModelList") or []):
                            mn = it.get("manageNumber", "")
                            vid = it.get("variantId", "") or ""
                            if not vid:
                                sl = it.get("SkuModelList") or []
                                if sl:
                                    vid = (sl[0].get("variantId") or
                                           sl[0].get("merchantDefinedSkuId") or "")
                            sku = f"{mn}:{vid}" if vid else mn
                            qty = int(it.get("units", 0))
                            if not sku or qty <= 0:
                                continue
                            if sku not in daily_by_sku:
                                daily_by_sku[sku] = {}
                            daily_by_sku[sku][odate_str] = daily_by_sku[sku].get(odate_str, 0) + qty
                if (i // 100 + 1) % 5 == 0:
                    logger.info("[楽天:%s] getOrder %d/%d 完了",
                                acc.name, min(i+100, len(order_nums)), len(order_nums))
                time.sleep(2.1)
        except Exception as e:
            logger.error("[楽天:%s] エラー: %s", acc.name, e)

    return daily_by_sku


# ─── スプレッドシート出力 ────────────────────────────────────────────────────
def write_to_sheet(amazon_daily: dict, rakuten_daily: dict, sku_to_name: dict,
                   days: int, share_emails: list, credentials_file: str,
                   folder_id: str = "") -> str:
    """新規スプレッドシートを作成して書き込み. URLを返す.

    folder_id を指定すると、そのDriveフォルダ内に作成（容量はフォルダ所有者を消費）。
    指定なしの場合はサービスアカウント直下に作成（容量制限あり）。
    """
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        credentials_file,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)

    title = f"販売実績_{datetime.now().strftime('%Y-%m-%d_%H%M')}"
    if folder_id:
        ss = gc.create(title, folder_id=folder_id)
        logger.info("✅ スプレッドシート作成 (フォルダID=%s): %s (id=%s)",
                    folder_id, title, ss.id)
    else:
        ss = gc.create(title)
        logger.info("✅ スプレッドシート作成: %s (id=%s)", title, ss.id)

    # 共有
    for email in share_emails:
        try:
            ss.share(email, perm_type="user", role="writer", notify=False)
            logger.info("  ✅ 共有: %s", email)
        except Exception as e:
            logger.warning("  共有失敗 %s: %s", email, e)

    # 日付列を生成（新しい順）
    end_d = date.today()
    date_cols = [(end_d - timedelta(days=i)).isoformat() for i in range(days)]

    # 全SKU
    all_skus = sorted(set(list(amazon_daily.keys()) + list(rakuten_daily.keys())))

    # ─── シート1: 全プラットフォーム合計 ───────────────────────────
    ws1 = ss.sheet1
    ws1.update_title("合計")
    rows1 = [["SKU", "商品名", f"合計({days}日)"] + date_cols]
    for sku in all_skus:
        a = amazon_daily.get(sku, {})
        r = rakuten_daily.get(sku, {})
        row = [sku, sku_to_name.get(sku, "")[:50]]
        total = 0
        date_vals = []
        for d in date_cols:
            v = (a.get(d, 0) or 0) + (r.get(d, 0) or 0)
            total += v
            date_vals.append(v if v > 0 else "")
        row.append(total)
        row.extend(date_vals)
        rows1.append(row)
    ws1.update(values=rows1, range_name="A1")
    logger.info("✅ 合計シート: %d SKU 書き込み完了", len(all_skus))

    # ─── シート2: Amazon ────────────────────────────────────────────
    ws2 = ss.add_worksheet(title="Amazon", rows=len(amazon_daily)+10, cols=days+5)
    rows2 = [["SKU", "商品名", f"合計({days}日)"] + date_cols]
    for sku in sorted(amazon_daily.keys()):
        a = amazon_daily[sku]
        row = [sku, sku_to_name.get(sku, "")[:50]]
        total = sum(a.values())
        row.append(total)
        for d in date_cols:
            row.append(a.get(d, 0) or "")
        rows2.append(row)
    ws2.update(values=rows2, range_name="A1")
    logger.info("✅ Amazonシート: %d SKU 書き込み完了", len(amazon_daily))

    # ─── シート3: 楽天 ──────────────────────────────────────────────
    ws3 = ss.add_worksheet(title="楽天", rows=len(rakuten_daily)+10, cols=days+5)
    rows3 = [["SKU", f"合計({days}日)"] + date_cols]
    for sku in sorted(rakuten_daily.keys()):
        r = rakuten_daily[sku]
        row = [sku, sum(r.values())]
        for d in date_cols:
            row.append(r.get(d, 0) or "")
        rows3.append(row)
    ws3.update(values=rows3, range_name="A1")
    logger.info("✅ 楽天シート: %d SKU 書き込み完了", len(rakuten_daily))

    return f"https://docs.google.com/spreadsheets/d/{ss.id}/edit"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90, help="過去日数（デフォルト90）")
    p.add_argument("--share", default="lilucaai02@gmail.com",
                   help="共有先メアド（カンマ区切り）")
    p.add_argument("--folder", default="",
                   help="作成先のDriveフォルダID（サービスアカウント容量回避）")
    p.add_argument("--cache-file", default="/tmp/sales_export_cache.json",
                   help="取得結果のJSON一時保存先")
    p.add_argument("--from-cache", action="store_true",
                   help="API取得をスキップしてキャッシュから書き込み")
    p.add_argument("--no-amazon", action="store_true")
    p.add_argument("--no-rakuten", action="store_true")
    args = p.parse_args()

    settings = Settings()

    amazon_daily: dict = {}
    rakuten_daily: dict = {}
    sku_to_name: dict = {}

    cache_path = Path(args.cache_file)
    if args.from_cache and cache_path.exists():
        print(f"\n=== キャッシュ読み込み: {cache_path} ===")
        c = json.loads(cache_path.read_text())
        amazon_daily = c.get("amazon", {})
        rakuten_daily = c.get("rakuten", {})
        sku_to_name = c.get("sku_to_name", {})
        print(f"  Amazon: {len(amazon_daily)} SKU, 楽天: {len(rakuten_daily)} SKU")
    else:
        if not args.no_amazon:
            print(f"\n=== Amazon: 過去{args.days}日 日別販売数取得 ===")
            t0 = time.time()
            amazon_daily, sku_to_name = fetch_amazon_daily(settings, args.days)
            print(f"  Amazon完了: {len(amazon_daily)} SKU ({time.time()-t0:.0f}秒)")

        if not args.no_rakuten:
            print(f"\n=== 楽天: 過去{args.days}日 日別販売数取得 ===")
            t0 = time.time()
            rakuten_daily = fetch_rakuten_daily(settings, args.days)
            print(f"  楽天完了: {len(rakuten_daily)} SKU ({time.time()-t0:.0f}秒)")

        # キャッシュ保存（失敗してもデータが残るように）
        cache_path.write_text(json.dumps({
            "amazon": amazon_daily,
            "rakuten": rakuten_daily,
            "sku_to_name": sku_to_name,
            "days": args.days,
            "saved_at": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2))
        print(f"\n💾 キャッシュ保存: {cache_path}")

    print(f"\n=== Googleスプレッドシート書き込み ===")
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "")
    if not creds_file or not os.path.exists(creds_file):
        print("ERROR: GOOGLE_CREDENTIALS_FILE が未設定または存在しません")
        sys.exit(1)

    share_emails = [e.strip() for e in args.share.split(",") if e.strip()]
    url = write_to_sheet(amazon_daily, rakuten_daily, sku_to_name,
                          args.days, share_emails, creds_file,
                          folder_id=args.folder)
    print(f"\n✅ 完了！スプレッドシート: {url}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
