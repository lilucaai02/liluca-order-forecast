#!/usr/bin/env python3
"""
全商品タブの日付を2027-03-31まで90日延長する。

手順 (タブごと):
 1. 90列を右端に追加し、行1に日付シリアルを書き込み
 2. 旧最終日列の数式を新90列へコピー (PASTE_FORMULA、相対参照は自動調整)
 3. イベント行・係数行は前年同期 (2026/1/1〜3/31) の値+書式をコピー
    (5と0のつく日・5のつく日・ゾロ目・スーパーセール・初売り等が同じ月日で再現)
 4. B列ヘルパーの範囲固定式 (発注個数予測の FILTER など) を開放範囲に修正

容量確保 (--prepare で実行):
 - 在庫/商品マスタ/発注と在庫移動一覧 の空き行・列の割当を削減
 - 商品タブの最古30日分 (2024-11-23〜12-22) の列を削除 (ユーザー指示による)

使い方:
  python3 extend_horizon_3months.py --prepare        # 容量確保のみ
  python3 extend_horizon_3months.py --tab "マウスピース(在庫)"
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)
EXT_START = datetime.date(2027, 1, 1)
EXT_END = datetime.date(2027, 3, 31)
DEL_OLD_FROM = datetime.date(2024, 11, 23)
DEL_OLD_TO = datetime.date(2024, 12, 22)   # この日まで削除 (30日分)
PROD_TABS = ["マウスピース(在庫)", "DS-01 (在庫) ", "TG-01(在庫)", "TG-02(在庫)",
             "GC-01(在庫)", "GC-02(在庫)", "PCI-01", "WB-01(在庫)", "WB-02",
             "TS-01", "PG-01"]
# 前年同期コピー対象のイベント/係数行ラベル
EVENT_LABELS = ["amazonイベント", "アマゾンイベント長沼", "amazonイベント係数",
                "アマゾンイベント係数長沼", "楽天イベント長沼", "楽天イベント係数長沼",
                "Yahooイベント長沼", "Yahooイベント係数長沼"]


def retry(fn, *a, **k):
    from gspread.exceptions import APIError
    delay = 20
    for i in range(9):
        try:
            return fn(*a, **k)
        except APIError as e:
            if any(x in str(e) for x in ("429", "500", "503")) and i < 8:
                time.sleep(delay)
                delay = min(delay * 2, 180)
            else:
                raise


def col_letter(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    settings = Settings()
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    return retry(gc.open_by_key, DEST_ID)


def get_dates(ws):
    row1 = retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1[0] if row1 else []
    return {i: BASE + datetime.timedelta(days=int(v))
            for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}


def prepare(sp):
    """容量確保: 空き割当の削減 + 商品タブの最古30列削除"""
    # 1) 空き行・列の割当削減 (削減対象が完全に空であることを実測確認)
    trims = [("在庫", "rows", 500), ("在庫", "cols", 2),
             ("商品マスタ", "rows", 100), ("発注と在庫移動一覧", "rows", 100)]
    for tab, kind, buf in trims:
        ws = retry(sp.worksheet, tab)
        if kind == "rows":
            used = max(len(retry(ws.col_values, c))
                       for c in range(1, min(ws.col_count, 28) + 1, 3))
            keep = used + buf
            if ws.row_count <= keep:
                print(f"{tab}: 行削減なし")
                continue
            # 削減対象領域が空であることを確認
            chk = retry(ws.get, f"A{keep + 1}:{col_letter(ws.col_count)}"
                                f"{min(keep + 200, ws.row_count)}")
            if any(any(str(x).strip() for x in row) for row in chk):
                print(f"{tab}: 行{keep + 1}以降にデータあり → スキップ")
                continue
            retry(sp.batch_update, {"requests": [{"deleteDimension": {
                "range": {"sheetId": ws.id, "dimension": "ROWS",
                          "startIndex": keep, "endIndex": ws.row_count}}}]})
            print(f"{tab}: 行 {ws.row_count}→{keep}")
        else:
            used = 0
            for probe in (1, 500, 1000, 2000, 3000, ws.row_count - 1):
                if probe < 1 or probe > ws.row_count:
                    continue
                row = (retry(ws.get, f"{probe}:{probe}") or [[]])[0]
                used = max(used, len(row))
            keep = used + buf
            if ws.col_count <= keep:
                print(f"{tab}: 列削減なし")
                continue
            chk = retry(ws.get, f"{col_letter(keep + 1)}1:"
                                f"{col_letter(ws.col_count)}{ws.row_count}")
            if any(any(str(x).strip() for x in row) for row in chk):
                print(f"{tab}: 列{keep + 1}以降にデータあり → スキップ")
                continue
            retry(sp.batch_update, {"requests": [{"deleteDimension": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": keep, "endIndex": ws.col_count}}}]})
            print(f"{tab}: 列 {ws.col_count}→{keep}")
    # 2) 商品タブ最古30列の削除 (2024-11-23〜12-22)
    for tab in PROD_TABS:
        ws = retry(sp.worksheet, tab)
        c2d = get_dates(ws)
        del_cols = [c for c, d in c2d.items() if DEL_OLD_FROM <= d <= DEL_OLD_TO]
        if not del_cols:
            print(f"{tab}: 削除対象なし (削除済み?)")
            continue
        lo, hi = min(del_cols), max(del_cols)
        assert hi - lo + 1 == len(del_cols) == 30, f"{tab}: 想定外 {del_cols}"
        assert c2d.get(lo) == DEL_OLD_FROM and c2d.get(hi) == DEL_OLD_TO
        retry(sp.batch_update, {"requests": [{"deleteDimension": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": lo - 1, "endIndex": hi}}}]})
        print(f"{tab}: 旧30列削除 ({DEL_OLD_FROM}〜{DEL_OLD_TO})")
    print("PREPARE DONE")


def extend(sp, tab):
    ws = retry(sp.worksheet, tab)
    c2d = get_dates(ws)
    last_c = max(c2d)
    last_d = c2d[last_c]
    if last_d >= EXT_END:
        print(f"[{tab}] 延長済み ({last_d})")
        return
    assert last_d == datetime.date(2026, 12, 31), f"{tab}: 最終日 {last_d}"
    n_new = (EXT_END - last_d).days           # 90
    new_first, new_last = last_c + 1, last_c + n_new

    # 1) 列追加
    need = new_last - ws.col_count
    if need > 0:
        retry(sp.batch_update, {"requests": [{"appendDimension": {
            "sheetId": ws.id, "dimension": "COLUMNS", "length": need}}]})
    # 2) 行1に日付
    dates = [(last_d + datetime.timedelta(days=i + 1) - BASE).days
             for i in range(n_new)]
    retry(ws.update,
          range_name=f"{col_letter(new_first)}1:{col_letter(new_last)}1",
          values=[dates], value_input_option='RAW')
    # 3) 旧最終列の数式を新90列にコピー (行2以降) + 書式コピー (行1含む)
    retry(sp.batch_update, {"requests": [
        {"copyPaste": {
            "source": {"sheetId": ws.id, "startRowIndex": 1,
                       "endRowIndex": ws.row_count,
                       "startColumnIndex": last_c - 1, "endColumnIndex": last_c},
            "destination": {"sheetId": ws.id, "startRowIndex": 1,
                            "endRowIndex": ws.row_count,
                            "startColumnIndex": new_first - 1,
                            "endColumnIndex": new_last},
            "pasteType": "PASTE_FORMULA"}},
        {"copyPaste": {
            "source": {"sheetId": ws.id, "startRowIndex": 0,
                       "endRowIndex": ws.row_count,
                       "startColumnIndex": last_c - 1, "endColumnIndex": last_c},
            "destination": {"sheetId": ws.id, "startRowIndex": 0,
                            "endRowIndex": ws.row_count,
                            "startColumnIndex": new_first - 1,
                            "endColumnIndex": new_last},
            "pasteType": "PASTE_FORMAT"}},
    ]})
    print(f"[{tab}] 90列追加+数式/書式コピー完了")

    # 4) イベント/係数行: 前年同期 (2026/1/1〜3/31) の値+書式をコピー
    c2d = get_dates(ws)
    d2c = {d: c for c, d in c2d.items()}
    src_a = d2c[datetime.date(2026, 1, 1)]
    src_b = d2c[datetime.date(2026, 3, 31)]
    dst_a = d2c[EXT_START]
    assert src_b - src_a == 89
    col_a = retry(ws.col_values, 1)
    ev_rows = [i for i, v in enumerate(col_a, 1)
               if v.strip() in EVENT_LABELS]
    reqs = []
    for r in ev_rows:
        reqs.append({"copyPaste": {
            "source": {"sheetId": ws.id, "startRowIndex": r - 1,
                       "endRowIndex": r, "startColumnIndex": src_a - 1,
                       "endColumnIndex": src_b},
            "destination": {"sheetId": ws.id, "startRowIndex": r - 1,
                            "endRowIndex": r, "startColumnIndex": dst_a - 1,
                            "endColumnIndex": dst_a - 1 + 90},
            "pasteType": "PASTE_NORMAL"}})
    for i in range(0, len(reqs), 60):
        retry(sp.batch_update, {"requests": reqs[i:i + 60]})
    print(f"[{tab}] イベント/係数 {len(ev_rows)}行に前年同期を展開")

    # 5) B列ヘルパーの範囲固定FILTER式を開放範囲に修正
    bcol = retry(ws.get, f"B1:B{ws.row_count}", value_render_option='FORMULA')
    fixes = []
    for i, row in enumerate(bcol, 1):
        f = row[0] if row else ""
        if not (isinstance(f, str) and f.startswith("=") and "FILTER(" in f):
            continue
        # 例: FILTER(IT30:ACQ30, ...) → FILTER($C$30:$30, ...)
        def fix_range(m):
            r1, r2 = m.group(2), m.group(4)
            if r1 == r2:
                return f"FILTER($C${r1}:${r2}"
            return m.group(0)
        nf = re.sub(r"FILTER\(\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)",
                    fix_range, f)
        nf = re.sub(r"\(\$?([A-Z]{1,3})\$?1:\$?([A-Z]{1,3})\$?1([>*<])",
                    r"($C$1:$1\3", nf)
        if nf != f:
            fixes.append({"range": f"B{i}", "values": [[nf]]})
    if fixes:
        for i in range(0, len(fixes), 100):
            retry(ws.batch_update, [dict(x) for x in fixes[i:i + 100]],
                  value_input_option='USER_ENTERED')
    print(f"[{tab}] B列ヘルパー修正 {len(fixes)}件")
    print(f"✅ [{tab}] 延長完了 (〜{EXT_END})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--tab")
    args = ap.parse_args()
    sp = open_sheet()
    if args.prepare:
        prepare(sp)
    elif args.tab:
        extend(sp, args.tab)
    else:
        ap.error("--prepare か --tab を指定")


if __name__ == "__main__":
    main()
