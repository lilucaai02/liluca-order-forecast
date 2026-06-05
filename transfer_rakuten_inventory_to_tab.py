#!/usr/bin/env python3
"""
日次楽天在庫推移シート → 各タブの「RSL在庫実績」行への転記（汎用）。

タブ別の設定は tab_blocks_config.py の TAB_BLOCKS で管理。
ブロックに `rsl_stock_row` が定義されていない場合はスキップ。

使い方:
  python3 transfer_rakuten_inventory_to_tab.py --tab "DS-01 (在庫) "
  python3 transfer_rakuten_inventory_to_tab.py --tab "マウスピース(在庫)" --date 2026-06-04
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import get_blocks

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SRC_SHEET_NAME = "日次楽天在庫推移"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def normalize_sku(sku: str) -> str:
    """楽天 SKU を商品コードと一致するように正規化（variantId 部分を抽出）。"""
    s = sku.lower().strip()
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\([^)]*\)", "", s)
    if ":" in s:
        s = s.split(":", 1)[1]
    return s.strip()


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_source_inventory(gc, src_spreadsheet_id: str, date_str: str) -> Dict[str, int]:
    """{正規化SKU: 合算qty}"""
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

    col_a = ws.col_values(1)
    n = len(col_a)
    target_col_letter = col_letter(target_col_idx)
    rows = ws.get(f"A2:{target_col_letter}{n}")

    aggregated: Dict[str, int] = {}
    for row in rows:
        if len(row) < 1:
            continue
        sku = row[0]
        if not sku:
            continue
        if len(row) >= target_col_idx:
            try:
                qty = int(row[target_col_idx - 1] or 0)
            except (ValueError, TypeError):
                qty = 0
        else:
            qty = 0
        key = normalize_sku(sku)
        aggregated[key] = aggregated.get(key, 0) + qty
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
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    blocks = get_blocks(args.tab)
    blocks_with_rsl = [b for b in blocks if "rsl_stock_row" in b]
    if not blocks_with_rsl:
        print(f"⚠ {args.tab} に rsl_stock_row 定義のブロックがありません", file=sys.stderr)
        return

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

    print(f"=== [{args.tab}] RSL在庫実績 転記 [{args.date}] ===", file=sys.stderr)

    by_norm = read_source_inventory(gc, settings.google_spreadsheet_id, args.date)
    print(f"元シート: {len(by_norm)}個の正規化SKU", file=sys.stderr)

    print(f"\n=== 商品コードごとの集計 ===", file=sys.stderr)
    block_totals: Dict[str, int] = {}
    for blk in blocks_with_rsl:
        norm_code = normalize_sku(blk["code"])
        total = by_norm.get(norm_code, 0)
        block_totals[blk["code"]] = total
        print(f"  {blk['code']}: {total}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(args.tab)
    date_col_idx = find_date_column_in_dest(dest_ws, args.date)
    date_col = col_letter(date_col_idx)
    print(f"\n転記先 {args.tab} の {args.date} = {date_col}列", file=sys.stderr)

    updates = []
    for blk in blocks_with_rsl:
        cell = f"{date_col}{blk['rsl_stock_row']}"
        updates.append({"range": cell, "values": [[block_totals[blk["code"]]]]})

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
