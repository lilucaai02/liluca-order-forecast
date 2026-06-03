#!/usr/bin/env python3
"""
Amazon 3アカウントの在庫を「縦=(SKU,アカウント)ペア、横=日付」で
Googleスプレッドシートに毎日記録する。

行は (SKU, アカウント) のペア単位。同じSKUが複数アカウントに登録されていれば
それぞれ別行として記録する。

シート構成（同一スプレッドシート内）:
  シート名: 「日次Amazon在庫推移」
  - A列: SKU
  - B列: アカウント名 (coconem / kk-trading / bulqrea)
  - 1行目: A1="SKU", B1="アカウント", C1以降=日付
  - C列以降: 各日付の在庫数（fulfillable_quantity）

動作:
  - 既存の(SKU,アカウント)ペアを読み、新規ペアが現れたら下に行を自動追加
  - 同日に2回実行された場合は同じ列を上書き
  - 新しい日付なら最終列の次に列追加

使い方:
  python3 daily_inventory_amazon_wide.py                  # 今日の日付で記録
  python3 daily_inventory_amazon_wide.py --date 2026-06-02 # 指定日付で記録
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.inventory import fetch_inventory
from src.sp_client import SPClient


SHEET_NAME = "日次Amazon在庫推移"

# key = (sku, account_name)
InvKey = Tuple[str, str]


def fetch_amazon_inventory_all(settings: Settings) -> Dict[InvKey, int]:
    """
    返り値: {(sku, account_name): fulfillable_qty, ...}
    各 (SKU, アカウント) ペアごとに1エントリ。
    """
    by_key: Dict[InvKey, int] = {}
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
        except Exception as e:
            print(f"[Amazon:{acc.name}] エラー: {e}", file=sys.stderr)
            continue
        for item in items:
            sku = item.seller_sku
            if not sku:
                continue
            qty = max(item.fulfillable_quantity, 0)
            by_key[(sku, acc.name)] = qty
        print(f"[Amazon:{acc.name}] {len(items)}件取得", file=sys.stderr)
    return by_key


def col_letter(n: int) -> str:
    """1始まりの列番号を A, B, ..., Z, AA, AB, ... に変換。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def ensure_sheet(spreadsheet, row_count: int):
    """シートを取得 or 作成。新規時は A1=SKU, B1=アカウント を入れる。"""
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
    """A列・B列を読んで、(SKU, アカウント)ペアのリストを順序保持で返す。"""
    col_a = ws.col_values(1)
    col_b = ws.col_values(2)
    keys: List[InvKey] = []
    # 1行目はヘッダー
    n = max(len(col_a), len(col_b))
    for i in range(1, n):
        sku = col_a[i] if i < len(col_a) else ""
        acc = col_b[i] if i < len(col_b) else ""
        if sku and acc:
            keys.append((sku, acc))
    return keys


def append_new_key_rows(ws, new_keys: List[InvKey], existing_count: int):
    """新規(SKU,アカウント)ペア行を A列・B列 に追加（1ペア = 1行）。"""
    if not new_keys:
        return
    start_row = 1 + existing_count + 1
    block: List[List[str]] = [[sku, acc] for (sku, acc) in new_keys]
    end_row = start_row + len(block) - 1

    if end_row > ws.row_count:
        ws.add_rows(end_row - ws.row_count + 100)

    rng = f"A{start_row}:B{end_row}"
    ws.update(range_name=rng, values=block)
    print(f"新規(SKU,アカウント)ペア {len(new_keys)}件 を追加（{start_row}〜{end_row}行）",
          file=sys.stderr)


def find_date_column(ws, date_str: str) -> int | None:
    """1行目（C列以降）から該当日付の列番号（1始まり）を返す。なければ None。"""
    row1 = ws.row_values(1)
    for idx, val in enumerate(row1, start=1):
        if idx < 3:
            continue
        if val == date_str:
            return idx
    return None


def build_column_values(keys: List[InvKey], by_key: Dict[InvKey, int], date_str: str) -> List[List]:
    """1列分の値を返す。最上段に日付、その下に各キーの qty。"""
    values: List[List] = [[date_str]]
    for key in keys:
        qty = by_key.get(key, 0)
        values.append([int(qty)])
    return values


def write_daily_column(spreadsheet, by_key: Dict[InvKey, int], date_str: str):
    ws = ensure_sheet(spreadsheet, len(by_key))

    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in by_key.keys() if k not in existing_set]
    if new_keys:
        append_new_key_rows(ws, new_keys, len(existing_keys))
        existing_keys = existing_keys + new_keys

    values = build_column_values(existing_keys, by_key, date_str)
    end_row = len(values)  # 1行目=日付 + ペア数

    target_col = find_date_column(ws, date_str)
    if target_col is None:
        row1 = ws.row_values(1)
        target_col = max(len(row1) + 1, 3)

    if target_col > ws.col_count:
        ws.add_cols(target_col - ws.col_count + 10)

    col = col_letter(target_col)
    rng = f"{col}1:{col}{end_row}"
    ws.update(range_name=rng, values=values)
    print(f"→ {date_str} を {col}列に書き込み（{end_row}行）", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="記録日付 (YYYY-MM-DD)。デフォルト: 今日")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .envに GOOGLE_CREDENTIALS_FILE と GOOGLE_SPREADSHEET_ID を設定してください",
              file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次Amazon在庫推移 [{args.date}] ===", file=sys.stderr)
    by_key = fetch_amazon_inventory_all(settings)
    if not by_key:
        print("Amazon在庫データが取れませんでした", file=sys.stderr)
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
    spreadsheet = gc.open_by_key(settings.google_spreadsheet_id)

    write_daily_column(spreadsheet, by_key, args.date)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
