#!/usr/bin/env python3
"""
販売実績行（amazonFBA販売実績 / 楽天販売実績 / Yahoo販売実績）の
「今日以降」のセルを空白化する。

未来日の実績セルには SUMIFS 等の数式が残っており 0 を返すため、
在庫予想の連鎖型計算や全体販売実績の集計を狂わせる。
今日以降を空白にすることで、実績が確定するまで「空欄」扱いになる。

対象行（A列ラベルで動的検出）:
  - amazonFBA販売実績
  - 楽天販売実績
  - Yahoo販売実績

使い方:
  python3 clear_future_actuals.py --tab "DS-01 (在庫) "        # 今日以降
  python3 clear_future_actuals.py --all
  python3 clear_future_actuals.py --tab "DS-01 (在庫) " --dry-run
  python3 clear_future_actuals.py --all --from-date 2026-06-11  # 指定日以降
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import TAB_BLOCKS

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)

TARGET_LABELS = ["amazonFBA販売実績", "楽天販売実績", "Yahoo販売実績"]


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


def find_date_column(ws, target_serial: int) -> int | None:
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    # その日以降の最初の列を返す（厳密一致がなければ次に大きい列）
    candidate = None
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) >= target_serial:
            if candidate is None or int(v) < candidate[1]:
                candidate = (i, int(v))
    return candidate[0] if candidate else None


def process_tab(ws, tab_name: str, start_serial: int, dry_run: bool):
    col_a = ws.col_values(1)
    target_rows = []
    for i, label in enumerate(col_a, start=1):
        if label.strip() in TARGET_LABELS:
            target_rows.append((i, label.strip()))

    if not target_rows:
        print(f"  [{tab_name}] 対象行なし", file=sys.stderr)
        return

    start_col = find_date_column(ws, start_serial)
    last_col = find_last_date_column(ws)
    if start_col is None or start_col > last_col:
        print(f"  [{tab_name}] 今日以降の列なし → スキップ", file=sys.stderr)
        return

    sc = col_letter(start_col)
    lc = col_letter(last_col)
    ncols = last_col - start_col + 1

    print(f"  [{tab_name}] 対象行={[r for r,_ in target_rows]} "
          f"範囲={sc}〜{lc} ({ncols}列)", file=sys.stderr)

    if dry_run:
        for r, label in target_rows:
            print(f"    R{r} ({label}): {sc}{r}:{lc}{r} を空白化予定", file=sys.stderr)
        return

    # 空白化（空文字を書き込む）
    empty_row = [[""] * ncols]
    for r, label in target_rows:
        ws.update(range_name=f"{sc}{r}:{lc}{r}", values=empty_row,
                  value_input_option='USER_ENTERED')
        print(f"    ✓ R{r} ({label}) 空白化", file=sys.stderr)
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--from-date", help="この日以降を空白化 (default: 今日)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.tab and not args.all:
        print("エラー: --tab または --all を指定", file=sys.stderr)
        sys.exit(1)

    if args.from_date:
        start_date = datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date()
    else:
        start_date = datetime.date.today()
    start_serial = (start_date - SERIAL_DATE_BASE).days

    settings = Settings()
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

    target_tabs = list(TAB_BLOCKS.keys()) if args.all else [args.tab]
    print(f"=== 販売実績 {start_date} 以降を空白化 ===", file=sys.stderr)

    for tab_name in target_tabs:
        try:
            ws = sp.worksheet(tab_name)
            process_tab(ws, tab_name, start_serial, args.dry_run)
        except Exception as e:
            print(f"  ⚠ [{tab_name}] エラー: {e}", file=sys.stderr)
        time.sleep(2)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
