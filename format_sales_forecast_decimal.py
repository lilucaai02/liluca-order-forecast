#!/usr/bin/env python3
"""
全商品タブの販売予想行の表示形式を「小数1桁」に変更。

対象行ラベル:
  - amazon販売予想
  - 楽天販売予想
  - Yahoo販売予想
  - 全体の販売予想

数式側の ROUND(..., 2) は変更せず、表示書式 "0.0" を適用するのみ。
（値の四捨五入は Sheets 側で表示時に行われる）

使い方:
  python3 format_sales_forecast_decimal.py              # 全11タブ
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"

TARGET_TABS = [
    "マウスピース(在庫)",
    "DS-01 (在庫) ",
    "GC-01(在庫)",
    "GC-02(在庫)",
    "TG-01(在庫)",
    "TG-02(在庫)",
    "PCI-01",
    "WB-01(在庫)",
    "WB-02",
    "TS-01",
    "PG-01",
]

TARGET_LABELS = {
    "amazon販売予想",
    "楽天販売予想",
    "Yahoo販売予想",
    "全体の販売予想",
}


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
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

    fmt_spec = {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}

    total_rows_formatted = 0
    for tab_name in TARGET_TABS:
        try:
            ws = sp.worksheet(tab_name)
        except Exception as e:
            print(f"⚠ [{tab_name}] 取得失敗: {e}", file=sys.stderr)
            continue

        col_a = ws.col_values(1)
        target_rows = [
            i for i, lab in enumerate(col_a, start=1)
            if lab.strip() in TARGET_LABELS
        ]

        if not target_rows:
            print(f"⚠ [{tab_name}] 対象行なし", file=sys.stderr)
            continue

        last_col = ws.col_count
        last_col_letter = col_letter(last_col)

        # 行単位で format 適用（複数行を1度に format するため batch）
        ranges = [f"C{r}:{last_col_letter}{r}" for r in target_rows]
        # gspread.format() は単一範囲のみなので複数回呼び出し（5列までまとめる）
        for rng in ranges:
            try:
                ws.format(rng, fmt_spec)
            except Exception as e:
                print(f"⚠ [{tab_name}] {rng} format 失敗: {e}", file=sys.stderr)
            time.sleep(0.5)

        total_rows_formatted += len(target_rows)
        print(f"✓ [{tab_name}] {len(target_rows)} 販売予想行を小数1桁書式に",
              file=sys.stderr)
        time.sleep(2)

    print(f"\n=== 合計 {total_rows_formatted} 行の表示形式を更新 ===")
    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
