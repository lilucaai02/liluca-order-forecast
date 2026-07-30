#!/usr/bin/env python3
"""ダッシュボード２「FBA在庫数」列の参照行を全商品で「FBA在庫実績」に揃える。

背景:
  DS-01 の行だけ FBA在庫数 が商品タブの「在庫予想」行を参照していた。
  「在庫予想」は未来日側にしか値が入らないため、TODAY()-1 を引くと空欄になり、
  ダッシュボードの FBA在庫数 が常に空欄表示になっていた。
  他の60商品はすべて「FBA在庫実績」行を参照している。

やること:
  1. ダッシュボード２のヘッダーから「FBA在庫数」列を特定
  2. 各行の数式から 参照タブ名 と 参照行番号 を抽出
  3. 商品タブのA列ラベルで「FBA在庫実績」の行番号を確定
  4. ずれている行だけ、他行と同じ形の数式に書き換える

使い方:
  python3 fix_dashboard_fba_stock_ref.py --dry-run
  python3 fix_dashboard_fba_stock_ref.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from config.settings import Settings
from fetch_safety import sheets_retry

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
DASH = "ダッシュボード２"
HEADER_FBA_STOCK = "FBA在庫数"
LABEL_FBA_ACTUAL = "FBA在庫実績"
LABEL_FORECAST = "在庫予想"

# 'タブ名'!$12:$12 形式の参照
REF_RE = re.compile(r"'([^']+)'!\$(\d+):\$(\d+)")


def a1col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def first_data_ref(formula: str):
    """行1(日付行)以外の最初の 'タブ'!$N:$N 参照を返す。"""
    if not formula or not formula.startswith("="):
        return None, None
    tab = None
    for m in REF_RE.finditer(formula):
        if tab is None:
            tab = m.group(1)
        if int(m.group(2)) != 1:
            return m.group(1), int(m.group(2))
    return tab, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import gspread
    from google.oauth2.service_account import Credentials

    settings = Settings()
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = sheets_retry(gc.open_by_key, DEST_ID)
    ws = sheets_retry(sp.worksheet, DASH)

    forms = sheets_retry(
        sp.values_get, f"'{DASH}'!A1:BZ{ws.row_count}",
        params={"valueRenderOption": "FORMULA"}).get("values", [])
    if not forms:
        print("ダッシュボードが空です", file=sys.stderr)
        return 1

    header = forms[0]
    try:
        col_idx = header.index(HEADER_FBA_STOCK)  # 0-based
    except ValueError:
        print(f"ヘッダー {HEADER_FBA_STOCK!r} が見つかりません: {header}", file=sys.stderr)
        return 1
    col_letter = a1col(col_idx + 1)
    print(f"FBA在庫数 列 = {col_letter} (1-based {col_idx + 1})")

    # 商品タブA列ラベルのキャッシュ
    label_cache: dict[str, list[str]] = {}

    def labels(tab: str) -> list[str]:
        if tab not in label_cache:
            wst = sheets_retry(sp.worksheet, tab)
            label_cache[tab] = sheets_retry(wst.col_values, 1)
        return label_cache[tab]

    def label_of(tab: str, row: int) -> str:
        la = labels(tab)
        return la[row - 1].strip() if 0 < row <= len(la) else ""

    fixes = []
    for r in range(2, len(forms) + 1):
        row = forms[r - 1]
        name = row[0] if row else ""
        if not name:
            continue
        f = row[col_idx] if len(row) > col_idx else ""
        tab, ref_row = first_data_ref(f)
        if not tab or not ref_row:
            continue
        cur_label = label_of(tab, ref_row)
        if cur_label == LABEL_FBA_ACTUAL:
            continue
        # 正しい行 = 同ブロック内の「FBA在庫実績」
        # 「在庫予想」を参照している場合はその直下を起点にラベル探索
        la = labels(tab)
        target = None
        for rr in range(ref_row, min(ref_row + 13, len(la) + 1)):
            lab = la[rr - 1].strip()
            if rr > ref_row and lab == LABEL_FORECAST:
                break
            if lab == LABEL_FBA_ACTUAL:
                target = rr
                break
        if target is None:
            print(f"  !! {name}: {tab} R{ref_row}({cur_label!r}) の近傍に "
                  f"{LABEL_FBA_ACTUAL} が見つかりません", file=sys.stderr)
            continue
        new_f = f.replace(f"'{tab}'!${ref_row}:${ref_row}",
                          f"'{tab}'!${target}:${target}")
        fixes.append({"row": r, "name": name, "tab": tab,
                      "from": ref_row, "from_label": cur_label,
                      "to": target, "old": f, "new": new_f})
        print(f"  修正対象 R{r} {name}: {tab} R{ref_row}({cur_label}) "
              f"→ R{target}({LABEL_FBA_ACTUAL})")

    if not fixes:
        print("修正対象なし (全行が FBA在庫実績 を参照しています)")
        return 0

    print(f"\n修正対象 {len(fixes)}件")
    for fx in fixes:
        print(f"  旧: {fx['old']}")
        print(f"  新: {fx['new']}")

    if args.dry_run:
        print("[dry-run] 書き込みは行いません")
        return 0

    data = [{"range": f"{col_letter}{fx['row']}", "values": [[fx["new"]]]}
            for fx in fixes]
    sheets_retry(ws.batch_update, data, value_input_option="USER_ENTERED")
    print("書き込み完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
