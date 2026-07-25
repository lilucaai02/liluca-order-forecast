#!/usr/bin/env python3
"""
日次Amazon販売推移シート → 大島さん用コピースプレッドシートの商品別タブへ転記。

転記先: 発注予測 のコピー20260722大島 (1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU)
行構造が既存(1mbZla...)と異なるため、専用のブロック定義を使う。

対応タブ・行構造 (2026-07-22 時点):
  DS-01 (在庫) :
    R1  = ASIN, R3 = 商品コード
    R9  = amazon販売予想        (sales_forecast_row)
    R10 = amazonFBA販売実績     (sales_row)          ← 本スクリプトの書き込み先
    R11 = 楽天販売予想
    R12 = 楽天販売実績
    R13 = Yahoo販売予想
    R14 = Yahoo販売実績
    R17 = FBA在庫予想
    R18 = FBA在庫実績
    R19 = RSL在庫予想
    R20 = RSL在庫実績
    R21 = Stock Crew在庫予想
    R22 = Stock Crew在庫実績

使い方:
  python3 transfer_sales_to_oshima_tab.py --tab "DS-01 (在庫) "
  python3 transfer_sales_to_oshima_tab.py --tab "DS-01 (在庫) " --dry-run
  python3 transfer_sales_to_oshima_tab.py --tab "DS-01 (在庫) " --from-date 2026-07-17 --to-date 2026-07-21
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from oshima_tab_blocks_config import OSHIMA_TAB_BLOCKS

DEST_SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
SRC_SHEET_NAME = "日次Amazon販売推移"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_sales_from_source(gc, src_spreadsheet_id: str,
                           dates: List[str]) -> Dict[Tuple[str, str], int]:
    """日次Amazon販売推移シートから (ASIN, 日付) → 全アカウント合算 qty。"""
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
        asin = row[0]
        if not asin:
            continue
        for date_str, col_idx in date_to_col.items():
            if len(row) >= col_idx:
                try:
                    qty = int(row[col_idx - 1] or 0)
                except (ValueError, TypeError):
                    qty = 0
            else:
                qty = 0
            key = (asin, date_str)
            result[key] = result.get(key, 0) + qty
    return result


def find_date_columns_bulk(ws, date_strs: List[str]) -> Dict[str, int]:
    """1回の read で複数日付の列位置をまとめて解決 (quota節約)。"""
    row1_raw = ws.get('1:1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    serial_to_col: Dict[int, int] = {}
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)):
            serial_to_col[int(v)] = i
    result: Dict[str, int] = {}
    missing = []
    for d in date_strs:
        target = datetime.datetime.strptime(d, "%Y-%m-%d").date()
        serial = (target - SERIAL_DATE_BASE).days
        if serial in serial_to_col:
            result[d] = serial_to_col[serial]
        else:
            missing.append((d, serial))
    if missing:
        raise ValueError(f"転記先タブに日付列が見つかりません: {missing}")
    return result


def find_date_column(ws, date_str: str) -> int:
    return find_date_columns_bulk(ws, [date_str])[date_str]


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

    blocks = OSHIMA_TAB_BLOCKS.get(args.tab)
    if not blocks:
        print(f"エラー: OSHIMA_TAB_BLOCKS に '{args.tab}' が定義されていません",
              file=sys.stderr)
        print(f"定義済みタブ: {list(OSHIMA_TAB_BLOCKS.keys())}", file=sys.stderr)
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

    from_d = datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_d = datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date()
    dates: List[str] = []
    d = from_d
    while d <= to_d:
        dates.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)

    print(f"=== [大島コピー / {args.tab}] amazonFBA販売実績 転記 "
          f"[{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)

    sales = read_sales_from_source(gc, settings.google_spreadsheet_id, dates)
    print(f"元シートから {len(sales)}件の (ASIN,日付) レコード", file=sys.stderr)

    asin_to_block = {blk["asin"]: blk for blk in blocks}
    print(f"\n=== ASIN → 商品コード マッピング ===", file=sys.stderr)
    for blk in blocks:
        print(f"  {blk['asin']} → {blk['code']}", file=sys.stderr)

    by_code_date: Dict[Tuple[str, str], int] = {}
    for (asin, d_str), qty in sales.items():
        blk = asin_to_block.get(asin)
        if not blk:
            continue
        key = (blk["code"], d_str)
        by_code_date[key] = by_code_date.get(key, 0) + qty

    print(f"\n=== 商品コード×日付 集計 ===", file=sys.stderr)
    for blk in blocks:
        for d_str in dates:
            qty = by_code_date.get((blk["code"], d_str), 0)
            print(f"  {blk['code']} / {d_str}: {qty}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(args.tab)
    date_to_col_dest: Dict[str, int] = find_date_columns_bulk(dest_ws, dates)

    updates = []
    for blk in blocks:
        for d_str in dates:
            qty = by_code_date.get((blk["code"], d_str), 0)
            col = col_letter(date_to_col_dest[d_str])
            updates.append({"range": f"{col}{blk['sales_row']}",
                            "values": [[qty]]})

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
