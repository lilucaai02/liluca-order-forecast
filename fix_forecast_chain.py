#!/usr/bin/env python3
"""
在庫予想を「連鎖型」に書き換える（汎用版）。

連鎖型の動作:
  在庫予想 = IF(前日実績="", 前日予想, 前日実績) + 移動入庫 - 販売予想 - 移動出庫
  - 前日の実績セルに値があれば、それを起点に予想
  - 前日の実績セルが空白なら、前日の予想を引き継ぐ
  - これにより未来日が連鎖して埋まる

同時に「未来日の実績セル(stock_row)」を空白化する（SUMIFS等の数式を削除）。
明日以降は実績が記録されたら自動で実績ベースに切替わる。

Amazon (--type amazon):
  対象: forecast_row (FBA在庫予想)
  数式: =IF(前日列{stock_row}="", 前日列{forecast_row}, 前日列{stock_row})
        + IF(前日列{to_row}="FBA",前日列{change_qty_row},0)
        - 前日列{sales_forecast_row}
        - IF(前日列{from_row}="FBA",前日列{change_qty_row},0)

楽天 (--type rakuten):
  対象: rsl_forecast_row (RSL在庫予想)
  数式: =IF(前日列{rsl_stock_row}="", 前日列{rsl_forecast_row}, 前日列{rsl_stock_row})
        + IF(前日列{to_row}="RSL",前日列{change_qty_row},0)
        - IF(今日列$1>TODAY(), 前日列{rakuten_sales_forecast_row}, 前日列{rakuten_sales_row})
        - IF(前日列{from_row}="RSL",前日列{change_qty_row},0)

使い方:
  python3 fix_forecast_chain.py --tab "DS-01 (在庫) " --type amazon
  python3 fix_forecast_chain.py --tab "DS-01 (在庫) " --type rakuten --dry-run
  python3 fix_forecast_chain.py --tab "マウスピース(在庫)" --type amazon --keep-stock-formula
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import get_blocks

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def find_last_date_column(ws) -> int:
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    last = 0
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and v > 40000:
            last = i
    return last


def find_date_column(ws, date_str: str) -> int:
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    target_serial = (target - SERIAL_DATE_BASE).days
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) == target_serial:
            return i
    raise ValueError(f"日付 '{date_str}' が見つかりません")


def build_chain_amazon(prev_col: str, blk: dict) -> str:
    return (
        f"=IF({prev_col}{blk['stock_row']}=\"\","
        f"{prev_col}{blk['forecast_row']},"
        f"{prev_col}{blk['stock_row']})"
        f"+IF({prev_col}{blk['to_row']}=\"FBA\",{prev_col}{blk['change_qty_row']},0)"
        f"-{prev_col}{blk['sales_forecast_row']}"
        f"-IF({prev_col}{blk['from_row']}=\"FBA\",{prev_col}{blk['change_qty_row']},0)"
    )


def build_chain_rakuten(prev_col: str, this_col: str, blk: dict) -> str:
    return (
        f"=IF({prev_col}{blk['rsl_stock_row']}=\"\","
        f"{prev_col}{blk['rsl_forecast_row']},"
        f"{prev_col}{blk['rsl_stock_row']})"
        f"+IF({prev_col}{blk['to_row']}=\"RSL\",{prev_col}{blk['change_qty_row']},0)"
        f"-IF({this_col}$1>TODAY(),"
        f"{prev_col}{blk['rakuten_sales_forecast_row']},"
        f"{prev_col}{blk['rakuten_sales_row']})"
        f"-IF({prev_col}{blk['from_row']}=\"RSL\",{prev_col}{blk['change_qty_row']},0)"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", required=True)
    parser.add_argument("--type", choices=["amazon", "rakuten"], default="amazon")
    parser.add_argument("--from-date",
                        help="開始日 (YYYY-MM-DD)。デフォルト: 明日")
    parser.add_argument("--keep-stock-formula", action="store_true",
                        help="未来日の在庫実績セルを空白化しない（既存数式を残す）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    blocks = get_blocks(args.tab)
    if args.type == "amazon":
        forecast_key = "forecast_row"
        stock_key = "stock_row"
        required = ["forecast_row", "stock_row", "sales_forecast_row",
                    "change_qty_row", "from_row", "to_row"]
        label = "FBA在庫予想"
    else:
        forecast_key = "rsl_forecast_row"
        stock_key = "rsl_stock_row"
        required = ["rsl_forecast_row", "rsl_stock_row",
                    "rakuten_sales_forecast_row", "rakuten_sales_row",
                    "change_qty_row", "from_row", "to_row"]
        label = "RSL在庫予想"

    valid_blocks = [b for b in blocks if all(k in b for k in required)]
    if not valid_blocks:
        print(f"⚠ {args.tab} に {args.type} 用必須行が揃ったブロックがありません", file=sys.stderr)
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
    sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    ws = sp.worksheet(args.tab)

    if args.from_date:
        start_date = args.from_date
    else:
        start_date = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    start_col_idx = find_date_column(ws, start_date)
    last_col_idx = find_last_date_column(ws)

    print(f"=== [{args.tab}] {label} 連鎖型書き換え (type={args.type}) ===", file=sys.stderr)
    print(f"開始日: {start_date} ({col_letter(start_col_idx)}列)", file=sys.stderr)
    print(f"最終列: {col_letter(last_col_idx)}列", file=sys.stderr)
    print(f"対象ブロック: {len(valid_blocks)} / 全 {len(blocks)}", file=sys.stderr)

    # 予想セルの更新
    forecast_updates = []
    for blk in valid_blocks:
        for col_idx in range(start_col_idx, last_col_idx + 1):
            prev_col = col_letter(col_idx - 1)
            this_col = col_letter(col_idx)
            if args.type == "amazon":
                formula = build_chain_amazon(prev_col, blk)
            else:
                formula = build_chain_rakuten(prev_col, this_col, blk)
            forecast_updates.append({
                "range": f"{this_col}{blk[forecast_key]}",
                "values": [[formula]]
            })

    # 実績セルの空白化（オプション）
    stock_updates = []
    if not args.keep_stock_formula:
        for blk in valid_blocks:
            for col_idx in range(start_col_idx, last_col_idx + 1):
                this_col = col_letter(col_idx)
                stock_updates.append({
                    "range": f"{this_col}{blk[stock_key]}",
                    "values": [[""]]
                })

    print(f"予想セル更新: {len(forecast_updates)}", file=sys.stderr)
    print(f"実績セル空白化: {len(stock_updates)} ({'スキップ' if args.keep_stock_formula else '実行'})",
          file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 予想数式 先頭3件:", file=sys.stderr)
        for u in forecast_updates[:3]:
            print(f"  {u['range']} ← {u['values'][0][0]}", file=sys.stderr)
        return

    # 一括書き込み
    BATCH = 200
    all_updates = forecast_updates + stock_updates
    total = 0
    for i in range(0, len(all_updates), BATCH):
        chunk = all_updates[i:i + BATCH]
        ws.batch_update(chunk, value_input_option='USER_ENTERED')
        total += len(chunk)
        print(f"  {total}/{len(all_updates)} セル書き込み済み", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
