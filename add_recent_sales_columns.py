#!/usr/bin/env python3
"""ダッシュボード２に「直近7日間の販売個数」「直近30日間の販売個数」を追加する。

加重平均と発注個数だけでは、実際にどれだけ売れているかが分からない。
直近の実売をそのまま並べて、発注量の判断材料にする。

数式で入れるので毎日の更新は不要。シートを開いた時点の直近N日を集計する。
集計元は各商品タブの「全体の販売実績」行 (Amazon＋楽天＋Yahoo)。
実績が入っていない日 (今日など) は空欄なので、自然に除外される。

使い方:
  python3 add_recent_sales_columns.py --dry-run
  python3 add_recent_sales_columns.py
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings                    # noqa: E402
from oshima_tab_blocks_config import OSHIMA_TAB_BLOCKS  # noqa: E402
from src.fetch_safety import sheets_retry, set_default_socket_timeout  # noqa: E402

SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
DASH = "ダッシュボード２"
LBL_SALES = "全体の販売実績"
ANCHOR = "平日販売数合計"        # この列の右隣に入れる
NEW_COLS = [("直近7日間の\n販売個数", 7), ("直近30日間の\n販売個数", 30)]

SKU_ALIAS_RAW = {
    "MP-02MHD4": "MP-02MHD", "PCI-01gray": "PCI-01gray1",
    "PG-01m": "pg-01ml", "PG-01l": "pg-01xl",
    "WB-01s": "S", "WB-01m": "M", "WB-01l": "L", "WB-01xl": "XL",
    "TS-01": "ts-01mw",
}


def norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).strip().lower()


ALIAS = {norm(k): norm(v) for k, v in SKU_ALIAS_RAW.items()}


def col_letter(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def main() -> None:
    p = argparse.ArgumentParser(description="ダッシュボード２に直近販売個数を追加")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    set_default_socket_timeout()
    settings = Settings()
    import gspread
    from google.oauth2.service_account import Credentials
    gc = gspread.authorize(Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]))
    sp = sheets_retry(gc.open_by_key, SPREADSHEET_ID)
    ws = sheets_retry(sp.worksheet, DASH)

    hdr = (sheets_retry(ws.get, "A1:BB1") or [[]])[0]
    for label, _ in NEW_COLS:
        if any(norm(h).startswith(norm(label.split("\n")[0])) for h in hdr):
            print(f"「{label.replace(chr(10), '')}」は既にあります。中止します。")
            return
    anchor = next((i for i, h in enumerate(hdr, 1) if ANCHOR in str(h)), None)
    if anchor is None:
        print(f"エラー: 「{ANCHOR}」列が見つかりません", file=sys.stderr)
        sys.exit(1)
    print(f"「{hdr[anchor-1]}」= {col_letter(anchor)}列 の右隣に2列追加します")

    # --- 商品タブの「全体の販売実績」行を引く -----------------------------
    row_of: Dict[str, Tuple[str, int]] = {}
    for tab, blocks in OSHIMA_TAB_BLOCKS.items():
        w = sheets_retry(sp.worksheet, tab)
        ca = sheets_retry(w.col_values, 1)
        bounds = [b["asin_row"] for b in blocks] + [len(ca) + 2]
        for bi, b in enumerate(blocks):
            lo, hi = bounds[bi], bounds[bi + 1]
            r = next((x for x in range(lo, hi)
                      if x - 1 < len(ca) and str(ca[x - 1]).strip() == LBL_SALES), None)
            if r:
                row_of[norm(b["code"])] = (tab, r)
    print(f"商品タブの参照先: {len(row_of)}件")

    names = sheets_retry(ws.get, "A2:A62")
    if args.dry_run:
        miss = [(r[0] if r else "") for r in names
                if r and r[0].strip()
                and norm(r[0]) not in row_of
                and ALIAS.get(norm(r[0]), "") not in row_of]
        print(f"[dry-run] 対象{len(names)}行 / 参照先が引けない行: {miss or 'なし'}")
        return

    # --- 列を2本挿入 -------------------------------------------------------
    sheets_retry(sp.batch_update, {"requests": [{"insertDimension": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                  "startIndex": anchor, "endIndex": anchor + len(NEW_COLS)},
        "inheritFromBefore": True}}]})
    print(f"{col_letter(anchor+1)}〜{col_letter(anchor+len(NEW_COLS))}列を挿入しました")

    ups: List[dict] = []
    unresolved: List[str] = []
    for k, (label, days) in enumerate(NEW_COLS):
        c = anchor + 1 + k
        ups.append({"range": f"{col_letter(c)}1", "values": [[label]]})
        for i, row in enumerate(names, start=2):
            nm = (row[0] if row else "").strip()
            if not nm:
                continue
            key = norm(nm)
            key = key if key in row_of else ALIAS.get(key, key)
            if key not in row_of:
                if k == 0:
                    unresolved.append(nm)
                continue
            tab, r = row_of[key]
            q = f"'{tab}'"
            # 今日は実績が未確定なので、昨日までのN日間を数える
            f = (f'=IFERROR(IF(COUNT(FILTER({q}!$C${r}:${r},'
                 f'{q}!$C$1:$1>=TODAY()-{days},{q}!$C$1:$1<TODAY()))=0,"−",'
                 f'ROUND(SUM(FILTER({q}!$C${r}:${r},'
                 f'{q}!$C$1:$1>=TODAY()-{days},{q}!$C$1:$1<TODAY())),0)),"−")')
            ups.append({"range": f"{col_letter(c)}{i}", "values": [[f]]})
    for i in range(0, len(ups), 200):
        sheets_retry(ws.batch_update, [dict(x) for x in ups[i:i + 200]],
                     value_input_option="USER_ENTERED")

    # --- 見た目を整える ----------------------------------------------------
    sheets_retry(sp.batch_update, {"requests": [
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": anchor, "endColumnIndex": anchor + len(NEW_COLS)},
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP", "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,"
                      "wrapStrategy,textFormat)"}},
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 62,
                      "startColumnIndex": anchor, "endColumnIndex": anchor + len(NEW_COLS)},
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"}},
    ]})
    print(f"{len(ups)}セル設定完了")
    if unresolved:
        print(f"[警告] 参照先が引けなかった行: {unresolved}")


if __name__ == "__main__":
    main()
