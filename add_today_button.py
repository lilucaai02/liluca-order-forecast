#!/usr/bin/env python3
"""
発注予測スプレッドシートの各商品タブの B1 セルに、
「今日の日付列へジャンプするボタン」（HYPERLINK 関数）を設置。

数式:
  =HYPERLINK(
    "https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={GID}&range="
    & LEFT(ADDRESS(1, MATCH(TODAY(), 1:1, 0), 4),
           LEN(ADDRESS(1, MATCH(TODAY(), 1:1, 0), 4)) - 1) & "1",
    "▶ 今日へ"
  )

仕組み:
  - MATCH(TODAY(), 1:1, 0): 1行目から TODAY() のシリアル日付が一致する列番号
  - ADDRESS(1, 列番号, 4): "A1" 形式の相対参照を返す
  - LEFT(..., LEN()-1): 末尾の行番号を除いて列名のみを取り出し
  - "1" を付けて該当列の1行目セルアドレスに
  - HYPERLINK で同スプレッドシートの該当 gid/range に移動

使い方:
  python3 add_today_button.py             # 全11タブに設置
"""

from __future__ import annotations

import os
import sys

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


def build_formula(sheet_id: str, gid: int, offset: int = 3) -> str:
    """
    今日の日付列より offset 列前にジャンプするボタンを返す。
    offset=0 なら今日の列が左端に、offset=3 なら3列前から見える。
    """
    base_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={gid}&range="
    # 今日の列番号 - offset で 3列前へジャンプ（最小1）
    addr_expr = f'ADDRESS(1,MAX(1,MATCH(TODAY(),1:1,0)-{offset}),4)'
    col_only = f'LEFT({addr_expr},LEN({addr_expr})-1)'
    return f'=HYPERLINK("{base_url}"&{col_only}&"1","▶ 今日へ")'


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

    tab_to_gid = {ws.title: ws.id for ws in sp.worksheets()}

    for tab_name in TARGET_TABS:
        if tab_name not in tab_to_gid:
            print(f"⚠ タブが見つかりません: {tab_name}", file=sys.stderr)
            continue
        gid = tab_to_gid[tab_name]
        formula = build_formula(DEST_SPREADSHEET_ID, gid)
        ws = sp.worksheet(tab_name)
        ws.update(range_name="B1", values=[[formula]], value_input_option='USER_ENTERED')
        print(f"✓ [{tab_name}] B1 にボタンを設置 (gid={gid})", file=sys.stderr)
        import time
        time.sleep(2)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
