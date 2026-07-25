#!/usr/bin/env python3
"""SP-APIプロモーションレポートから未来のタイムセール予定を取得し、
「タイムセール入力」シートに追記する。

取得対象: APPROVED の LIGHTNING_DEAL / BEST_DEAL (未来または実施中)
ASIN→商品コードは oshima_tab_blocks_config で解決。
既に同じ商品コード+開始日の行があればスキップ (重複追加しない)。

その後 apply_timesale_schedule.py を実行すると各商品タブの
アマゾンイベント長沼/係数長沼 行に反映される。

使い方:
  python3 fetch_amazon_deals.py [--apply]   # --apply で反映まで一括実行
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.sp_client import SPClient
import oshima_tab_blocks_config

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
INPUT_SHEET = "タイムセール入力"
JST = datetime.timezone(datetime.timedelta(hours=9))
TABS = ["マウスピース(在庫)", "DS-01 (在庫) ", "TG-01(在庫)", "TG-02(在庫)",
        "GC-01(在庫)", "GC-02(在庫)", "PCI-01", "WB-01(在庫)", "WB-02",
        "TS-01", "PG-01"]


def retry_g(fn, *a, **k):
    from gspread.exceptions import APIError
    delay = 20
    for i in range(9):
        try:
            return fn(*a, **k)
        except APIError as e:
            if any(x in str(e) for x in ("429", "500", "503")) and i < 8:
                time.sleep(delay)
                delay = min(delay * 2, 180)
            else:
                raise


def fetch_report() -> list[dict]:
    from sp_api.api import Reports
    settings = Settings()
    sp = SPClient(settings)
    reports = sp._make_client(Reports)
    now = datetime.datetime.now(datetime.timezone.utc)
    res = reports.create_report(
        reportType="GET_PROMOTION_PERFORMANCE_REPORT",
        marketplaceIds=[sp.marketplace_id],
        reportOptions={
            "promotionStartDateFrom": (now - datetime.timedelta(days=7)).isoformat(),
            "promotionStartDateTo": (now + datetime.timedelta(days=120)).isoformat(),
        })
    rid = res.payload["reportId"]
    doc_id = None
    for _ in range(40):
        time.sleep(15)
        st = reports.get_report(rid)
        status = st.payload.get("processingStatus")
        if status == "DONE":
            doc_id = st.payload.get("reportDocumentId")
            break
        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"レポート生成失敗: {status}")
    doc = reports.get_report_document(doc_id, download=True, decrypt=True)
    payload = doc.payload
    text = payload.get("document") if isinstance(payload, dict) else payload
    data = json.loads(text) if isinstance(text, str) else text
    return data.get("promotions", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    promos = fetch_report()
    now = datetime.datetime.now(datetime.timezone.utc)
    deals = []
    for p in promos:
        if p.get("status") != "APPROVED":
            continue
        if p.get("type") not in ("LIGHTNING_DEAL", "BEST_DEAL"):
            continue
        end = datetime.datetime.fromisoformat(
            p["endDateTime"].replace("Z", "+00:00"))
        if end < now:
            continue
        d0 = datetime.datetime.fromisoformat(
            p["startDateTime"].replace("Z", "+00:00")).astimezone(JST).date()
        d1 = end.astimezone(JST).date()
        asins = [x["asin"] for x in p.get("includedProducts", [])]
        deals.append({"d0": d0, "d1": d1, "type": p["type"], "asins": asins})
    print(f"未来/実施中のAPPROVEDセール: {len(deals)}件")

    # ASIN → 商品コード
    asin_map = {}
    for tab in TABS:
        for b in oshima_tab_blocks_config.get_blocks(tab):
            asin_map.setdefault(b["asin"], b["code"])

    settings = Settings()
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = retry_g(gc.open_by_key, DEST_ID)
    wsi = retry_g(sp.worksheet, INPUT_SHEET)
    existing = retry_g(wsi.get, "A3:C60")
    seen = set()
    for row in existing:
        row = (row + ["", "", ""])[:3]
        if str(row[0]).strip():
            seen.add((str(row[0]).strip().lower(), str(row[1]).strip()))

    new_rows = []
    unknown = set()
    for d in deals:
        for asin in d["asins"]:
            code = asin_map.get(asin)
            if not code:
                unknown.add(asin)
                continue
            key = (code.lower(), d["d0"].strftime("%Y/%m/%d"))
            if key in seen:
                continue
            seen.add(key)
            new_rows.append([code, d["d0"].strftime("%Y/%m/%d"),
                             d["d1"].strftime("%Y/%m/%d"),
                             "タイムセール", "", ""])
    if unknown:
        print("コード不明ASIN:", sorted(unknown))
    if new_rows:
        start = 3 + len([r for r in existing if any(str(x).strip() for x in r)])
        retry_g(wsi.update,
                range_name=f"A{start}:F{start + len(new_rows) - 1}",
                values=new_rows, value_input_option='USER_ENTERED')
        print(f"入力シートに {len(new_rows)}行 追加:")
        for r in new_rows:
            print("  ", r[:4])
    else:
        print("追加なし (すべて登録済み)")

    if args.apply and new_rows:
        print("--- 反映実行 ---")
        subprocess.run([sys.executable, "apply_timesale_schedule.py"],
                       cwd=os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    main()
