#!/usr/bin/env python3
"""
全商品タブの「Amazon平日平均販売点数」行に、直近30日のイベント外日平均を返す
数式を一括で書き込む。

数式 (全列同じ):
  =IFERROR(MAX(0, AVERAGEIFS(
    {sales_row}:{sales_row},          # amazonFBA販売実績 行全体
    {event_row}:{event_row}, "",      # amazonイベント 行が空欄
    1:1, ">="&TODAY()-{N},            # 直近 N 日
    1:1, "<"&TODAY()                  # 今日を含めない
  )), 0)

行番号は tab_blocks_config から動的取得:
  amazon_avg_row   = sales_forecast_row - 1
  amazon_event_row = sales_forecast_row - 5 (rakuten_event_row 定義あり)
                   or sales_forecast_row - 3 (それ以外)
  sales_row        = blk["sales_row"]

使い方:
  python3 set_amazon_avg_formula.py --tab "DS-01 (在庫) " --days 30
  python3 set_amazon_avg_formula.py --tab "マウスピース(在庫)" --days 30
  python3 set_amazon_avg_formula.py --all --days 30
  python3 set_amazon_avg_formula.py --tab "DS-01 (在庫) " --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import TAB_BLOCKS, get_blocks

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


def get_amazon_avg_row(blk: dict) -> int:
    return blk["sales_forecast_row"] - 1


def get_amazon_event_row(blk: dict) -> int:
    # 楽天イベント行が追加された構造 (DS-01等) は -5、それ以外は -3
    if "rakuten_event_row" in blk:
        return blk["sales_forecast_row"] - 5
    return blk["sales_forecast_row"] - 3


def build_formula(blk: dict, days: int) -> str:
    avg_row = get_amazon_avg_row(blk)
    event_row = get_amazon_event_row(blk)
    sales_row = blk["sales_row"]
    return (
        f"=IFERROR(MAX(0,AVERAGEIFS("
        f"{sales_row}:{sales_row},"
        f"{event_row}:{event_row},\"\","
        f"1:1,\">=\"&(TODAY()-{days}),"
        f"1:1,\"<\"&TODAY()"
        f")),0)"
    )


def apply_to_tab(ws, blocks, days: int, dry_run: bool):
    last_col_idx = find_last_date_column(ws)
    print(f"  最終列: {col_letter(last_col_idx)} ({last_col_idx})", file=sys.stderr)

    updates = []
    for blk in blocks:
        avg_row = get_amazon_avg_row(blk)
        event_row = get_amazon_event_row(blk)
        sales_row = blk["sales_row"]
        formula = build_formula(blk, days)
        # C列〜最終列まで全部に同じ数式
        for ci in range(3, last_col_idx + 1):
            cell = f"{col_letter(ci)}{avg_row}"
            updates.append({"range": cell, "values": [[formula]]})
        print(f"    {blk['code']}: 平均行=R{avg_row}, イベント行=R{event_row}, 実績行=R{sales_row}",
              file=sys.stderr)

    print(f"  書き込み対象セル: {len(updates)}", file=sys.stderr)

    if dry_run:
        print(f"\n[dry-run] サンプル数式:", file=sys.stderr)
        if updates:
            print(f"  {updates[0]['range']} ← {updates[0]['values'][0][0]}", file=sys.stderr)
        return

    BATCH = 500
    total = 0
    for i in range(0, len(updates), BATCH):
        chunk = updates[i:i + BATCH]
        ws.batch_update(chunk, value_input_option='USER_ENTERED')
        total += len(chunk)
        print(f"  {total}/{len(updates)} セル書き込み済み", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", help="対象タブ名（--all との併用不可）")
    parser.add_argument("--all", action="store_true", help="全11タブに適用")
    parser.add_argument("--days", type=int, default=30, help="集計期間日数 (default: 30)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.tab and not args.all:
        print("エラー: --tab または --all を指定してください", file=sys.stderr)
        sys.exit(1)

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
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
    sp = gc.open_by_key(DEST_SPREADSHEET_ID)

    if args.all:
        target_tabs = list(TAB_BLOCKS.keys())
    else:
        target_tabs = [args.tab]

    for tab_name in target_tabs:
        print(f"\n=== [{tab_name}] (days={args.days}) ===", file=sys.stderr)
        try:
            ws = sp.worksheet(tab_name)
            blocks = get_blocks(tab_name)
            apply_to_tab(ws, blocks, args.days, args.dry_run)
        except Exception as e:
            print(f"  ⚠ エラー: {e}", file=sys.stderr)
        time.sleep(3)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
