#!/usr/bin/env python3
"""
在庫予想 (FBA在庫予想 / RSL在庫予想) が 0 のセルに赤背景を付ける条件付き書式を設定。

各タブの全ブロックについて、forecast_row (FBA在庫予想) と
rsl_forecast_row (RSL在庫予想) の C列〜最終列の範囲に
「セル値 = 0 なら赤背景」のルールを追加する。

0 が複数日続けば、その全日に赤が付く（条件付き書式なので動的）。

使い方:
  python3 highlight_zero_forecast.py --tab "DS-01 (在庫) "
  python3 highlight_zero_forecast.py --all
  python3 highlight_zero_forecast.py --tab "DS-01 (在庫) " --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import TAB_BLOCKS, get_blocks

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"

# 赤背景色
RED_BG = {"red": 0.96, "green": 0.80, "blue": 0.80}


def find_last_date_column(ws) -> int:
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    last = 0
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and v > 40000:
            last = i
    return last


def build_rules_for_tab(ws, sheet_id: int, blocks: list, last_col_idx: int) -> list:
    """各ブロックの予想行に対する条件付き書式ルール (addConditionalFormatRule) を生成。"""
    requests = []
    for blk in blocks:
        forecast_rows = []
        if "forecast_row" in blk:
            forecast_rows.append(blk["forecast_row"])
        if "rsl_forecast_row" in blk:
            forecast_rows.append(blk["rsl_forecast_row"])

        for frow in forecast_rows:
            # C列(=index2) 〜 最終列。0始まりindex
            grid_range = {
                "sheetId": sheet_id,
                "startRowIndex": frow - 1,
                "endRowIndex": frow,
                "startColumnIndex": 2,           # C列
                "endColumnIndex": last_col_idx,  # 最終列(0始まりなのでlast_col_idxまで)
            }
            rule = {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [grid_range],
                        "booleanRule": {
                            "condition": {
                                "type": "NUMBER_EQ",
                                "values": [{"userEnteredValue": "0"}],
                            },
                            "format": {"backgroundColor": RED_BG},
                        },
                    },
                    "index": 0,
                }
            }
            requests.append(rule)
    return requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.tab and not args.all:
        print("エラー: --tab または --all を指定", file=sys.stderr)
        sys.exit(1)

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

    for tab_name in target_tabs:
        print(f"\n=== [{tab_name}] ===", file=sys.stderr)
        try:
            ws = sp.worksheet(tab_name)
            blocks = get_blocks(tab_name)
            last_col = find_last_date_column(ws)
            requests = build_rules_for_tab(ws, ws.id, blocks, last_col)
            print(f"  ルール数: {len(requests)} (最終列={last_col})", file=sys.stderr)

            if args.dry_run:
                print(f"  [dry-run] 予想行: "
                      f"{[(b['code'], b.get('forecast_row'), b.get('rsl_forecast_row')) for b in blocks]}",
                      file=sys.stderr)
                continue

            # batchUpdate で一括適用
            sp.batch_update({"requests": requests})
            print(f"  ✓ {len(requests)} ルール適用", file=sys.stderr)
        except Exception as e:
            print(f"  ⚠ エラー: {e}", file=sys.stderr)
        time.sleep(3)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
