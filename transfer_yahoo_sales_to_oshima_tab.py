#!/usr/bin/env python3
"""
日次Yahoo販売推移シート → 大島コピー各タブの「Yahoo販売実績」行への転記。

転記先: 発注予測 のコピー20260722大島 (1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU)
タブ・ブロック定義: oshima_tab_blocks_config.OSHIMA_TAB_BLOCKS

Yahoo SKU は「item_id:sub_code」形式（または item_id のみ）。

使い方:
  python3 transfer_yahoo_sales_to_oshima_tab.py --tab "DS-01 (在庫) "
  python3 transfer_yahoo_sales_to_oshima_tab.py --tab "マウスピース(在庫)" --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from oshima_tab_blocks_config import get_blocks

DEST_SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
SRC_SHEET_NAME = "日次Yahoo販売推移"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def normalize_sku(sku: str) -> str:
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


def read_sales_from_source(gc, src_spreadsheet_id: str,
                           dates: List[str]) -> Dict[Tuple[str, str], int]:
    sp = gc.open_by_key(src_spreadsheet_id)
    ws = sp.worksheet(SRC_SHEET_NAME)

    row1 = ws.row_values(1)
    date_to_col: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if v in dates:
            date_to_col[v] = i

    missing = [d for d in dates if d not in date_to_col]
    if missing:
        raise ValueError(f"元シートに日付列がありません: {missing}")

    last_col = max(date_to_col.values())
    last_col_letter = col_letter(last_col)
    all_data = ws.get(f"A2:{last_col_letter}")

    result: Dict[Tuple[str, str], int] = {}
    for row in all_data:
        if len(row) < 1:
            continue
        sku = row[0]
        if not sku:
            continue
        norm = normalize_sku(sku)
        for date_str, col_idx in date_to_col.items():
            if len(row) >= col_idx:
                try:
                    qty = int(row[col_idx - 1] or 0)
                except (ValueError, TypeError):
                    qty = 0
            else:
                qty = 0
            key = (norm, date_str)
            result[key] = result.get(key, 0) + qty
    return result


def find_date_column(ws, date_str: str) -> int:
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
    today = datetime.date.today()
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=5)).strftime("%Y-%m-%d"))
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    blocks = get_blocks(args.tab)
    blocks_with_yahoo = [b for b in blocks if "yahoo_sales_row" in b]
    if not blocks_with_yahoo:
        print(f"⚠ {args.tab} に yahoo_sales_row 定義のブロックがありません",
              file=sys.stderr)
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

    from_d = datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_d = datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date()
    dates: List[str] = []
    d = from_d
    while d <= to_d:
        dates.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)

    print(f"=== [大島コピー / {args.tab}] Yahoo販売実績 転記 "
          f"[{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)

    sales = read_sales_from_source(gc, settings.google_spreadsheet_id, dates)
    print(f"元シートから {len(sales)}件の (正規化SKU,日付) レコード", file=sys.stderr)

    by_code_date: Dict[Tuple[str, str], int] = {}
    print(f"\n=== 商品コード×日付 集計 ===", file=sys.stderr)
    for blk in blocks_with_yahoo:
        norm_code = normalize_sku(blk["code"])
        for d_str in dates:
            qty = sales.get((norm_code, d_str), 0)
            by_code_date[(blk["code"], d_str)] = qty
            print(f"  {blk['code']} / {d_str}: {qty}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(args.tab)
    date_to_col_dest: Dict[str, int] = {d: find_date_column(dest_ws, d) for d in dates}

    updates = []
    for blk in blocks_with_yahoo:
        for d_str in dates:
            qty = by_code_date.get((blk["code"], d_str), 0)
            col = col_letter(date_to_col_dest[d_str])
            updates.append({"range": f"{col}{blk['yahoo_sales_row']}",
                            "values": [[qty]]})

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
