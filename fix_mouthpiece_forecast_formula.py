#!/usr/bin/env python3
"""
マウスピース(在庫) タブのFBA在庫予想セル (R17, R69, R120, R172, R224, R276, R327) を
「前日の実績(R18等)を起点にした数式」に書き換える。

修正前: =前日列17 + IF(前日列40="FBA",前日列38,0) - 前日列9 - IF(前日列39="FBA",前日列38,0)
修正後: =前日列18 + IF(前日列40="FBA",前日列38,0) - 前日列9 - IF(前日列39="FBA",前日列38,0)

対象範囲:
  UO列（今日UN列の翌日）から最終列まで。
  最終列はシート1行目の有効データの末尾を自動検出。

使い方:
  python3 fix_mouthpiece_forecast_formula.py            # 実行
  python3 fix_mouthpiece_forecast_formula.py --dry-run  # 何セル更新するかだけ表示
  python3 fix_mouthpiece_forecast_formula.py --from-date 2026-06-04  # 開始日指定
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
DEST_SHEET_NAME = "マウスピース(在庫)"

# (商品コード, 予想行, 実績行, 変更数量行, FROM行, TO行, 販売予想行)
BLOCKS: List[Tuple[str, int, int, int, int, int, int]] = [
    ("MP-02MHD",         17,  18,  38,  39,  40,   9),
    ("MP-02MHD6",        69,  70,  90,  91,  92,  61),
    ("MP-03",           120, 121, 141, 142, 143, 112),
    ("MP-01",           172, 173, 193, 194, 195, 164),
    ("MP-02",           224, 225, 245, 246, 247, 216),
    ("MP-02MHD-small",  276, 277, 297, 298, 299, 268),
    ("MP-04",           327, 328, 348, 349, 350, 319),
]


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def find_last_date_column(ws) -> int:
    """1行目のシリアル日付が入っている最後の列番号を返す。"""
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    last = 0
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and v > 40000:  # 2009年以降のシリアル
            last = i
    return last


def find_date_column(ws, date_str: str) -> int:
    base = datetime.date(1899, 12, 30)
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    target_serial = (target - base).days
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) == target_serial:
            return i
    raise ValueError(f"日付 '{date_str}' (serial={target_serial}) が見つかりません")


def build_formula(prev_col: str, actual_row: int, change_qty_row: int,
                  from_row: int, to_row: int, sales_forecast_row: int) -> str:
    return (
        f"={prev_col}{actual_row}"
        f"+IF({prev_col}{to_row}=\"FBA\",{prev_col}{change_qty_row},0)"
        f"-{prev_col}{sales_forecast_row}"
        f"-IF({prev_col}{from_row}=\"FBA\",{prev_col}{change_qty_row},0)"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date",
                        help="開始日 (YYYY-MM-DD)。デフォルト: 明日")
    parser.add_argument("--dry-run", action="store_true",
                        help="書き込みせず、対象セル数のみ表示")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認してください", file=sys.stderr)
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
    ws = sp.worksheet(DEST_SHEET_NAME)

    # 開始日
    if args.from_date:
        start_date = args.from_date
    else:
        # 今日の翌日
        start_date = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    start_col_idx = find_date_column(ws, start_date)
    last_col_idx = find_last_date_column(ws)

    print(f"=== FBA在庫予想数式 一括書き換え ===", file=sys.stderr)
    print(f"開始日: {start_date} ({col_letter(start_col_idx)}列, {start_col_idx}列目)", file=sys.stderr)
    print(f"最終列: {col_letter(last_col_idx)}列 ({last_col_idx}列目)", file=sys.stderr)
    print(f"対象ブロック: {len(BLOCKS)} 個", file=sys.stderr)

    updates = []
    for code, forecast_row, actual_row, change_qty_row, from_row, to_row, sales_row in BLOCKS:
        for col_idx in range(start_col_idx, last_col_idx + 1):
            prev_col = col_letter(col_idx - 1)
            this_col = col_letter(col_idx)
            formula = build_formula(prev_col, actual_row, change_qty_row,
                                    from_row, to_row, sales_row)
            updates.append({
                "range": f"{this_col}{forecast_row}",
                "values": [[formula]]
            })

    print(f"対象セル数: {len(updates)}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 先頭3件の例:", file=sys.stderr)
        for u in updates[:3]:
            print(f"  {u['range']} ← {u['values'][0][0]}", file=sys.stderr)
        return

    # batch_update は一度に大量の数式を扱うと遅いので、適度に分割
    BATCH_SIZE = 200
    total_written = 0
    for i in range(0, len(updates), BATCH_SIZE):
        chunk = updates[i:i + BATCH_SIZE]
        ws.batch_update(chunk, value_input_option='USER_ENTERED')
        total_written += len(chunk)
        print(f"  {total_written}/{len(updates)} セル書き込み済み", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
