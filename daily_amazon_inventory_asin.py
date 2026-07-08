#!/usr/bin/env python3
"""
Amazon 3アカウントの FBA在庫を「縦=(ASIN,アカウント)、横=日付」で
Googleスプレッドシートに毎日記録する（ASINベース）。

同じ ASIN に複数 SKU がある場合は合算し、C列に対応SKUをカンマ区切りで記録。
販売推移シート(daily_amazon_sales.py)と同じ構造。

シート構成:
  シート名: 「日次Amazon在庫推移」
  - A列: ASIN
  - B列: アカウント名
  - C列: 対応SKU (同じASIN×アカウントの全SKUをカンマ区切り)
  - 1行目: A1="ASIN", B1="アカウント", C1="対応SKU", D1以降=日付
  - D列以降: 各日付の在庫数 (fulfillable_quantity, ASIN×アカウント合算)

使い方:
  python3 daily_amazon_inventory_asin.py                  # 今日
  python3 daily_amazon_inventory_asin.py --date 2026-06-10
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.inventory import fetch_inventory
from src.sp_client import SPClient

SHEET_NAME = "日次Amazon在庫推移"
InvKey = Tuple[str, str]  # (asin, account_name)


def fetch_inventory_by_asin(settings: Settings):
    """
    返り値:
      qty_map: {(asin, account_name): fulfillable_qty合算}
      sku_map: {(asin, account_name): [SKUリスト]}
    """
    qty_map: Dict[InvKey, int] = {}
    sku_map: Dict[InvKey, List[str]] = {}
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
        except Exception as e:
            print(f"[Amazon:{acc.name}] エラー: {e}", file=sys.stderr)
            continue
        for item in items:
            asin = item.asin
            if not asin:
                continue
            key = (asin, acc.name)
            qty_map[key] = qty_map.get(key, 0) + max(item.fulfillable_quantity, 0)
            if item.seller_sku:
                sku_map.setdefault(key, []).append(item.seller_sku)
        print(f"[Amazon:{acc.name}] {len(items)}件取得", file=sys.stderr)
    return qty_map, sku_map


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
        ws.update(range_name="A1:C1", values=[["ASIN", "アカウント", "対応SKU"]])
        ws.freeze(rows=1, cols=3)
        print(f"シート「{SHEET_NAME}」を新規作成しました", file=sys.stderr)
    return ws


def read_existing_keys(ws) -> List[InvKey]:
    col_a = ws.col_values(1)
    col_b = ws.col_values(2)
    keys: List[InvKey] = []
    n = max(len(col_a), len(col_b))
    for i in range(1, n):
        a = col_a[i] if i < len(col_a) else ""
        b = col_b[i] if i < len(col_b) else ""
        if a and b:
            keys.append((a, b))
    return keys


def append_new_key_rows(ws, new_keys: List[InvKey],
                        sku_map: Dict[InvKey, List[str]], existing_count: int):
    if not new_keys:
        return
    start_row = 1 + existing_count + 1
    block = []
    for (a, b) in new_keys:
        skus = ", ".join(sorted(set(sku_map.get((a, b), []))))
        block.append([a, b, skus])
    end_row = start_row + len(block) - 1
    if end_row > ws.row_count:
        ws.add_rows(end_row - ws.row_count + 100)
    ws.update(range_name=f"A{start_row}:C{end_row}", values=block)
    print(f"新規(ASIN,アカウント)ペア {len(new_keys)}件 を追加（{start_row}〜{end_row}行）",
          file=sys.stderr)


def update_sku_column(ws, all_keys: List[InvKey], sku_map: Dict[InvKey, List[str]]):
    if not all_keys:
        return
    updates = []
    for idx, key in enumerate(all_keys, start=2):
        skus = ", ".join(sorted(set(sku_map.get(key, []))))
        updates.append({"range": f"C{idx}", "values": [[skus]]})
    BATCH = 200
    for i in range(0, len(updates), BATCH):
        ws.batch_update(updates[i:i+BATCH], value_input_option='USER_ENTERED')


def find_date_column(ws, date_str: str) -> int | None:
    row1 = ws.row_values(1)
    for idx, val in enumerate(row1, start=1):
        if idx < 4:
            continue
        if val == date_str:
            return idx
    return None


def write_daily_column(spreadsheet, qty_map: Dict[InvKey, int],
                       sku_map: Dict[InvKey, List[str]], date_str: str):
    all_keys = sorted(qty_map.keys())
    ws = ensure_sheet(spreadsheet, len(all_keys))

    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in all_keys if k not in existing_set]
    if new_keys:
        append_new_key_rows(ws, new_keys, sku_map, len(existing_keys))
        existing_keys = existing_keys + new_keys

    # SKU列を最新化
    update_sku_column(ws, existing_keys, sku_map)

    target_col = find_date_column(ws, date_str)
    if target_col is None:
        row1 = ws.row_values(1)
        target_col = max(len(row1) + 1, 4)
    if target_col > ws.col_count:
        ws.add_cols(target_col - ws.col_count + 10)

    values: List[List] = [[date_str]]
    for key in existing_keys:
        values.append([int(qty_map.get(key, 0))])

    col = col_letter(target_col)
    ws.update(range_name=f"{col}1:{col}{len(values)}", values=values)
    print(f"→ {date_str} を {col}列に書き込み（{len(values)}行）", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.date.today().strftime("%Y-%m-%d"))
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次Amazon在庫推移(ASIN) [{args.date}] ===", file=sys.stderr)
    qty_map, sku_map = fetch_inventory_by_asin(settings)
    if not qty_map:
        print("在庫データが取れませんでした", file=sys.stderr)
        sys.exit(1)
    print(f"対象 (ASIN,アカウント) ペア数: {len(qty_map)}", file=sys.stderr)

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
    write_daily_column(sp, qty_map, sku_map, args.date)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
