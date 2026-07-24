#!/usr/bin/env python3
"""
Yahoo!ショッピング ストア管理API getStock → 「日次Yahoo在庫推移」シート

在庫スナップショットを日次で記録（今日の列に上書き）。

シート構成:
  シート名: 「日次Yahoo在庫推移」
  - A列: SKU (item_id もしくは item_id:sub_code)
  - B列: アカウント名
  - 1行目: A1="SKU", B1="アカウント", C1以降=日付
  - C列以降: 各日付の在庫数（空欄=無制限は -1）

使い方:
  python3 daily_yahoo_inventory.py                  # 今日
  python3 daily_yahoo_inventory.py --date 2026-07-22
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.yahoo_client import YahooClient

SHEET_NAME = "日次Yahoo在庫推移"
InvKey = Tuple[str, str]  # (sku, account_name)


def fetch_yahoo_stock(settings: Settings) -> Dict[InvKey, int]:
    """全Yahooアカウントの (sku, account_name) → 在庫数 を取得."""
    result: Dict[InvKey, int] = {}
    for acc in settings.get_yahoo_accounts():
        if not acc.refresh_token or not acc.client_secret:
            print(f"[Yahoo:{acc.name}] refresh_token / client_secret 未設定 → スキップ",
                  file=sys.stderr)
            continue
        try:
            client = YahooClient(
                account_name=acc.name,
                client_id=acc.client_id,
                seller_id=acc.seller_id,
                client_secret=acc.client_secret,
                refresh_token=acc.refresh_token,
            )
            # 全SKU一覧を itemSearch (V3公開API) で取得
            items = client.get_store_items()
            item_codes: list[str] = []
            for item in items:
                sku = client.extract_sku(item)
                if sku and sku not in item_codes:
                    item_codes.append(sku)
            print(f"[Yahoo:{acc.name}] itemSearch: {len(item_codes)} SKU", file=sys.stderr)

            # getStock で実在庫数を取得
            stock_map = client.get_store_stock(item_codes)
            for sku, qty in stock_map.items():
                result[(sku, acc.name)] = max(qty, 0) if qty >= 0 else -1
            print(f"[Yahoo:{acc.name}] getStock: {len(stock_map)} 件取得", file=sys.stderr)
        except Exception as e:
            print(f"[Yahoo:{acc.name}] エラー: {e}", file=sys.stderr)
            continue
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
        print(f"シート「{SHEET_NAME}」を新規作成", file=sys.stderr)
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
    ws.update(range_name=f"A{start_row}:B{end_row}", values=block)
    print(f"新規(SKU,アカウント)ペア {len(new_keys)}件 を追加", file=sys.stderr)


def find_date_column(ws, date_str: str) -> int | None:
    row1 = ws.row_values(1)
    for idx, val in enumerate(row1, start=1):
        if idx < 3:
            continue
        if val == date_str:
            return idx
    return None


def write_daily_column(spreadsheet, by_key: Dict[InvKey, int], date_str: str):
    all_keys = sorted(by_key.keys())
    ws = ensure_sheet(spreadsheet, len(all_keys))

    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in all_keys if k not in existing_set]
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
    if not settings.get_yahoo_accounts():
        print("エラー: Yahooアカウント未設定", file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次Yahoo在庫推移 [{args.date}] ===", file=sys.stderr)
    by_key = fetch_yahoo_stock(settings)
    if not by_key:
        print("在庫データが取れませんでした", file=sys.stderr)
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
