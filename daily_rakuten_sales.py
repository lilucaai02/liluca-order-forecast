#!/usr/bin/env python3
"""
楽天 RMS API → 「日次楽天販売推移」シート（縦=SKU×アカウント、横=日付）

楽天は SKU 単位で注文を取得できるため、Amazon と違い ASIN マッピングは不要。
SKU 形式: manageNumber:variantId （variantId が無ければ manageNumber のみ）

シート構成:
  シート名: 「日次楽天販売推移」
  - A列: SKU
  - B列: アカウント名
  - 1行目: A1="SKU", B1="アカウント", C1以降=日付
  - C列以降: 各日付の販売数

使い方:
  python3 daily_rakuten_sales.py                                       # 昨日と一昨日
  python3 daily_rakuten_sales.py --from-date 2026-06-02 --to-date 2026-06-03
"""

from __future__ import annotations

import argparse
import base64
import datetime
import os
import sys
import time
from typing import Dict, List, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

SHEET_NAME = "日次楽天販売推移"
SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/"
GET_URL    = "https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/"

SalesKey = Tuple[str, str]  # (sku, account_name)


def fetch_rakuten_sales(
    settings: Settings,
    from_date: str,
    to_date: str,
) -> Dict[Tuple[str, str, str], int]:
    """{(sku, account_name, date_str): qty, ...} を返す。"""
    result: Dict[Tuple[str, str, str], int] = {}

    for acc in settings.get_rakuten_accounts():
        credential = f"{acc.service_secret}:{acc.license_key}"
        encoded = base64.b64encode(credential.encode()).decode()
        headers = {
            "Authorization": f"ESA {encoded}",
            "Content-Type": "application/json; charset=utf-8",
        }

        # searchOrder で注文番号一覧
        all_order_nums: List[str] = []
        page = 1
        while True:
            payload = {
                "dateType": 1,
                "startDatetime": f"{from_date}T00:00:00+0900",
                "endDatetime":   f"{to_date}T23:59:59+0900",
                "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page},
            }
            resp = requests.post(SEARCH_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                print(f"[楽天:{acc.name}] searchOrder失敗 status={resp.status_code}: {resp.text[:200]}",
                      file=sys.stderr)
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

        all_order_nums = list(dict.fromkeys(all_order_nums))
        print(f"[楽天:{acc.name}] 注文番号 {len(all_order_nums)}件取得", file=sys.stderr)

        # getOrder で注文詳細
        for i in range(0, len(all_order_nums), 100):
            batch = all_order_nums[i:i+100]
            payload = {"orderNumberList": batch, "version": 7}
            resp = requests.post(GET_URL, headers=headers, json=payload, timeout=60)
            if resp.status_code != 200:
                print(f"[楽天:{acc.name}] getOrder失敗 status={resp.status_code}",
                      file=sys.stderr)
                time.sleep(5)
                continue
            d = resp.json()
            for o in (d.get("OrderModelList") or []):
                odate_str = (o.get("orderDatetime") or "")[:10]
                if not odate_str:
                    continue
                for pkg in (o.get("PackageModelList") or []):
                    for item in (pkg.get("ItemModelList") or []):
                        mn = item.get("manageNumber", "")
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
                        key = (sku, acc.name, odate_str)
                        result[key] = result.get(key, 0) + qty
            time.sleep(2.1)
        print(f"[楽天:{acc.name}] getOrder 取得完了", file=sys.stderr)

    return result


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def ensure_sheet(spreadsheet, row_count: int):
    import gspread
    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        rows = max(1 + row_count, 500)
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=rows, cols=400)
        ws.update(range_name="A1:B1", values=[["SKU", "アカウント"]])
        ws.freeze(rows=1, cols=2)
        print(f"シート「{SHEET_NAME}」を新規作成しました", file=sys.stderr)
    return ws


def read_existing_keys(ws) -> List[SalesKey]:
    col_a = ws.col_values(1)
    col_b = ws.col_values(2)
    keys: List[SalesKey] = []
    n = max(len(col_a), len(col_b))
    for i in range(1, n):
        a = col_a[i] if i < len(col_a) else ""
        b = col_b[i] if i < len(col_b) else ""
        if a and b:
            keys.append((a, b))
    return keys


def append_new_key_rows(ws, new_keys: List[SalesKey], existing_count: int):
    if not new_keys:
        return
    start_row = 1 + existing_count + 1
    block = [[a, b] for (a, b) in new_keys]
    end_row = start_row + len(block) - 1
    if end_row > ws.row_count:
        ws.add_rows(end_row - ws.row_count + 100)
    rng = f"A{start_row}:B{end_row}"
    ws.update(range_name=rng, values=block)
    print(f"新規(SKU,アカウント)ペア {len(new_keys)}件 を追加（{start_row}〜{end_row}行）", file=sys.stderr)


def get_date_columns(ws) -> Dict[str, int]:
    row1 = ws.row_values(1)
    result: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if i < 3:
            continue
        if v:
            result[v] = i
    return result


def write_sales(spreadsheet, sales: Dict[Tuple[str, str, str], int], dates: List[str]):
    all_keys_set = set()
    for (sku, acc, _d) in sales:
        all_keys_set.add((sku, acc))
    all_keys = sorted(all_keys_set)

    ws = ensure_sheet(spreadsheet, len(all_keys))

    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in all_keys if k not in existing_set]
    if new_keys:
        append_new_key_rows(ws, new_keys, len(existing_keys))
        existing_keys = existing_keys + new_keys

    date_cols = get_date_columns(ws)
    row1 = ws.row_values(1)
    next_col = max(len(row1) + 1, 3)
    new_date_updates = []
    for d in dates:
        if d not in date_cols:
            date_cols[d] = next_col
            new_date_updates.append({"range": f"{col_letter(next_col)}1", "values": [[d]]})
            next_col += 1
    if new_date_updates:
        if next_col - 1 > ws.col_count:
            ws.add_cols((next_col - 1) - ws.col_count + 10)
        ws.batch_update(new_date_updates, value_input_option='USER_ENTERED')
        print(f"新規日付列 {len(new_date_updates)}件 を追加", file=sys.stderr)

    key_to_row = {k: idx for idx, k in enumerate(existing_keys, start=2)}

    updates = []
    for key in existing_keys:
        row = key_to_row[key]
        for d in dates:
            if d not in date_cols:
                continue
            col = col_letter(date_cols[d])
            qty = sales.get((key[0], key[1], d), 0)
            updates.append({"range": f"{col}{row}", "values": [[qty]]})

    if updates:
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            ws.batch_update(chunk, value_input_option='USER_ENTERED')
        print(f"→ {len(updates)}セル書き込み（販売 {len(sales)}件 + 0埋め）", file=sys.stderr)


def daterange(start_date: datetime.date, end_date: datetime.date) -> List[str]:
    out = []
    d = start_date
    while d <= end_date:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def main():
    parser = argparse.ArgumentParser()
    today = datetime.date.today()
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=2)).strftime("%Y-%m-%d"))
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    if not settings.get_rakuten_accounts():
        print("エラー: 楽天アカウント未設定", file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次楽天販売推移 [{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)
    sales = fetch_rakuten_sales(settings, args.from_date, args.to_date)
    print(f"取得した販売レコード: {len(sales)}件", file=sys.stderr)

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    sp = gc.open_by_key(settings.google_spreadsheet_id)

    dates = daterange(
        datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date(),
        datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date(),
    )
    write_sales(sp, sales, dates)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
