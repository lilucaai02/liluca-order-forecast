#!/usr/bin/env python3
"""
日次Amazon在庫推移シート(ASIN版) → 商品別タブの「FBA在庫実績」行への転記（汎用）。

元シート「日次Amazon在庫推移」は ASIN ベース:
  A=ASIN, B=アカウント, C=対応SKU, D列以降=日付

各タブのブロック (tab_blocks_config) の asin で照合し、
同じ ASIN の全アカウント在庫を合算して stock_row に書き込む。
（販売転記 transfer_sales_to_tab.py と同じ ASINマッチ方式）

使い方:
  python3 transfer_inventory_to_tab.py --tab "マウスピース(在庫)"
  python3 transfer_inventory_to_tab.py --tab "DS-01 (在庫) " --date 2026-06-11
  python3 transfer_inventory_to_tab.py --tab "DS-01 (在庫) " --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import get_blocks

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SRC_SHEET_NAME = "日次Amazon在庫推移"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_source_inventory_by_asin(gc, src_spreadsheet_id: str, date_str: str) -> Dict[str, int]:
    """日次Amazon在庫推移(ASIN版)から指定日の {ASIN: 全アカウント合算qty} を返す。"""
    sp = gc.open_by_key(src_spreadsheet_id)
    ws = sp.worksheet(SRC_SHEET_NAME)

    row1 = ws.row_values(1)
    target_col_idx = None
    for i, v in enumerate(row1, start=1):
        if v == date_str:
            target_col_idx = i
            break
    if target_col_idx is None:
        raise ValueError(f"元シート1行目に日付 '{date_str}' が見つかりません")

    last_col_letter = col_letter(target_col_idx)
    rows = ws.get(f"A2:{last_col_letter}")

    aggregated: Dict[str, int] = {}
    for row in rows:
        if len(row) < 1:
            continue
        asin = row[0]
        if not asin:
            continue
        if len(row) >= target_col_idx:
            try:
                qty = int(row[target_col_idx - 1] or 0)
            except (ValueError, TypeError):
                qty = 0
        else:
            qty = 0
        aggregated[asin] = aggregated.get(asin, 0) + qty
    return aggregated


def find_date_column_in_dest(ws, date_str: str) -> int:
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    target_serial = (target - SERIAL_DATE_BASE).days
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) == target_serial:
            return i
    raise ValueError(f"転記先タブに日付 '{date_str}' (serial={target_serial}) が見つかりません")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", required=True)
    parser.add_argument("--date",
                        default=datetime.date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    blocks = get_blocks(args.tab)

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

    print(f"=== [{args.tab}] FBA在庫実績 転記(ASIN) [{args.date}] ===", file=sys.stderr)

    # 1. 元シートから指定日の ASIN→在庫合算
    by_asin = read_source_inventory_by_asin(gc, settings.google_spreadsheet_id, args.date)
    print(f"元シート: {len(by_asin)}個のASINを集計", file=sys.stderr)

    # 2. ブロックの asin で集計
    print(f"\n=== ASIN→商品コード 集計 ===", file=sys.stderr)
    block_totals: Dict[str, int] = {}
    for blk in blocks:
        asin = blk["asin"]
        total = by_asin.get(asin, 0)
        block_totals[blk["code"]] = total
        print(f"  {blk['code']} ({asin}): {total}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    # 3. 転記先タブの日付列を特定して書き込み
    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(args.tab)
    date_col_idx = find_date_column_in_dest(dest_ws, args.date)
    date_col = col_letter(date_col_idx)
    print(f"\n転記先 {args.tab} の {args.date} = {date_col}列", file=sys.stderr)

    updates = []
    for blk in blocks:
        cell = f"{date_col}{blk['stock_row']}"
        updates.append({"range": cell, "values": [[block_totals[blk["code"]]]]})

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
