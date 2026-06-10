#!/usr/bin/env python3
"""
日次Amazon販売推移シート → 発注予測スプレッドシートの商品別タブの
「amazonFBA販売実績」行への転記（汎用版）。

タブ別の設定は tab_blocks_config.py の TAB_BLOCKS で管理。

使い方:
  python3 transfer_sales_to_tab.py --tab "マウスピース(在庫)"
  python3 transfer_sales_to_tab.py --tab "DS-01 (在庫) "
  python3 transfer_sales_to_tab.py --tab "マウスピース(在庫)" --from-date 2026-06-01 --to-date 2026-06-02
  python3 transfer_sales_to_tab.py --tab "マウスピース(在庫)" --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import get_blocks


DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
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
    # 元シート側で過去5日を再取得しているので、転記も同じ範囲で上書き
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

    # 日付リスト
    from_d = datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_d = datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date()
    dates: List[str] = []
    d = from_d
    while d <= to_d:
        dates.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)

    print(f"=== [{args.tab}] amazonFBA販売実績 転記 [{args.from_date} ～ {args.to_date}] ===",
          file=sys.stderr)

    # 1. 元シートから ASIN×日付の販売数
    sales = read_sales_from_source(gc, settings.google_spreadsheet_id, dates)
    print(f"元シートから {len(sales)}件の (ASIN,日付) レコード", file=sys.stderr)

    # 2. ASIN→商品コード マッピング (タブ設定から)
    asin_to_block = {blk["asin"]: blk for blk in blocks}
    print(f"\n=== ASIN → 商品コード マッピング ===", file=sys.stderr)
    for blk in blocks:
        print(f"  {blk['asin']} → {blk['code']}", file=sys.stderr)

    # 3. 商品コード×日付ごとに集計
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

    # 4. 転記先タブの各日付列を取得して書き込み
    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(args.tab)
    date_to_col_dest: Dict[str, int] = {d: find_date_column(dest_ws, d) for d in dates}

    updates = []
    for blk in blocks:
        for d_str in dates:
            qty = by_code_date.get((blk["code"], d_str), 0)
            col = col_letter(date_to_col_dest[d_str])
            updates.append({"range": f"{col}{blk['sales_row']}", "values": [[qty]]})

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
