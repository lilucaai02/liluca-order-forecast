#!/usr/bin/env python3
"""
楽天 RMS Inventory API → 「日次楽天在庫推移」シート

シート構成:
  シート名: 「日次楽天在庫推移」
  - A列: SKU (manageNumber:variantId)
  - B列: アカウント名
  - 1行目: A1="SKU", B1="アカウント", C1以降=日付
  - C列以降: 各日付の在庫数

使い方:
  python3 daily_rakuten_inventory.py                  # 今日
  python3 daily_rakuten_inventory.py --date 2026-06-04
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.inventory import fetch_rakuten_inventory
from src.rakuten_client import RakutenClient

SHEET_NAME = "日次楽天在庫推移"

InvKey = Tuple[str, str]  # (sku, account_name)


def fetch_rakuten_all(settings: Settings) -> Dict[InvKey, int]:
    """{(sku, account_name): qty} を返す。"""
    by_key: Dict[InvKey, int] = {}
    for acc in settings.get_rakuten_accounts():
        try:
            client = RakutenClient(acc)
            items = fetch_rakuten_inventory(client)
        except Exception as e:
            print(f"[楽天:{acc.name}] エラー: {e}", file=sys.stderr)
            continue
        for item in items:
            sku = item.seller_sku
            if not sku:
                continue
            by_key[(sku, acc.name)] = max(item.fulfillable_quantity, 0)
        print(f"[楽天:{acc.name}] {len(items)}件取得", file=sys.stderr)
    return by_key


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


def append_new_key_rows(ws, new_keys: List[InvKey], existing_count: int):
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


def find_date_column(ws, date_str: str) -> int | None:
    row1 = ws.row_values(1)
    for idx, val in enumerate(row1, start=1):
        if idx < 3:
            continue
        if val == date_str:
            return idx
    return None


def write_daily_column(spreadsheet, by_key: Dict[InvKey, int], date_str: str):
    ws = ensure_sheet(spreadsheet, len(by_key))

    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in by_key.keys() if k not in existing_set]
    if new_keys:
        append_new_key_rows(ws, new_keys, len(existing_keys))
        existing_keys = existing_keys + new_keys

    target_col = find_date_column(ws, date_str)
    if target_col is None:
        row1 = ws.row_values(1)
        target_col = max(len(row1) + 1, 3)

    if target_col > ws.col_count:
        ws.add_cols(target_col - ws.col_count + 10)

    values: List[List] = [[date_str]]
    for key in existing_keys:
        qty = by_key.get(key, 0)
        values.append([int(qty)])

    col = col_letter(target_col)
    rng = f"{col}1:{col}{len(values)}"
    ws.update(range_name=rng, values=values)
    print(f"→ {date_str} を {col}列に書き込み（{len(values)}行）", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",
                        default=datetime.date.today().strftime("%Y-%m-%d"))
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)
    if not settings.get_rakuten_accounts():
        print("エラー: 楽天アカウント未設定", file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次楽天在庫推移 [{args.date}] ===", file=sys.stderr)
    by_key = fetch_rakuten_all(settings)
    if not by_key:
        print("楽天在庫データが取れませんでした", file=sys.stderr)
        sys.exit(1)

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

    write_daily_column(sp, by_key, args.date)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
