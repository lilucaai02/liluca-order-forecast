#!/usr/bin/env python3
"""
全商品タブの「Amazon平日平均販売点数」行の表示形式を小数第一位 (0.0) にする。

数式 (AVERAGEIFS) はそのまま、表示書式のみ "0.0" を適用。

使い方:
  python3 format_avg_decimal.py            # 全11タブ
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import TAB_BLOCKS

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
TARGET_LABELS = {"Amazon平日平均販売点数"}


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
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

    fmt_spec = {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}
    total = 0

    for tab_name in TAB_BLOCKS.keys():
        try:
            ws = sp.worksheet(tab_name)
        except Exception as e:
            print(f"⚠ [{tab_name}] 取得失敗: {e}", file=sys.stderr)
            continue

        col_a = ws.col_values(1)
        target_rows = [i for i, lab in enumerate(col_a, start=1)
                       if lab.strip() in TARGET_LABELS]
        if not target_rows:
            print(f"⚠ [{tab_name}] 対象行なし", file=sys.stderr)
            continue

        last_col = col_letter(ws.col_count)
        for r in target_rows:
            try:
                ws.format(f"C{r}:{last_col}{r}", fmt_spec)
            except Exception as e:
                print(f"⚠ [{tab_name}] R{r} format失敗: {e}", file=sys.stderr)
            time.sleep(0.5)

        total += len(target_rows)
        print(f"✓ [{tab_name}] {len(target_rows)}行を小数第一位に", file=sys.stderr)
        time.sleep(2)

    print(f"\n=== 合計 {total} 行の表示形式を更新 ===")
    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
