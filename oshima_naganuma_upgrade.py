#!/usr/bin/env python3
"""
大島コピーの商品タブに DS-01 と同じ「長沼」拡張を一括適用する。

各ブロックに対して:
  1. 行挿入 (8行/ブロック):
     - amazonイベント の下: アマゾンイベント長沼 / 直近7平日セール以外平均 / 過去30日セール以外平均
     - amazonイベント係数 の下: アマゾンイベント係数長沼
     - amazon販売予想 の下: アマゾン長沼予想７日 / アマゾン長沼予想３０日
     - FBA在庫予想 の下: 在庫予想長沼７日 / 在庫予想長沼３０日
  2. 日付軸を 12/31 まで延長 (タブ単位、未延長の場合)
  3. アマゾンイベント長沼行を「amazonイベント行(文字+色) ∪ セール価格行の緑」から構築
     + 未来セール (スマイル/感謝祭予想/BF予想) を適用
  4. 販売実績 (Amazon/楽天) を元シートから全期間再転記、明日以降は空白化
  5. FBA在庫実績の空き日を元シート在庫推移から補完
  6. 係数長沼: 過去=期間ごとの実測、未来=同種の直近5回平均
  7. 数式行: 平均/予想/在庫予想 (すべて開放範囲・行末まで)
  8. oshima_tab_blocks_config.py の行番号を更新

使い方:
  python3 oshima_naganuma_upgrade.py --tab "TG-01(在庫)"
  python3 oshima_naganuma_upgrade.py --tab "TG-01(在庫)" --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import statistics
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
import oshima_tab_blocks_config

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
SRC_ID = "12Di9y6pwb7CI39GKpR9QPNKCtCqEilTNGjDXVUmjq6c"
BASE = datetime.date(1899, 12, 30)
TARGET_END = datetime.date(2026, 12, 31)

WHITE = {"red": 1, "green": 1, "blue": 1}
BLACK = {"red": 0, "green": 0, "blue": 0}
ORANGE = {"red": 1.0, "green": 0.85, "blue": 0.4}
GREEN2 = {"red": 0.71, "green": 0.88, "blue": 0.65}
BLUE = {"red": 0.62, "green": 0.77, "blue": 0.91}
LAVEND = {"red": 0.85, "green": 0.82, "blue": 0.91}
PURPLE = {"red": 0.71, "green": 0.65, "blue": 0.84}
PURE_GREEN = (0, 1, 0)

FUTURE_SALES = [
    (datetime.date(2026, 8, 28), datetime.date(2026, 9, 3),
     "スマイルセール", ORANGE),
    (datetime.date(2026, 10, 9), datetime.date(2026, 10, 11),
     "先行セール（予想）", GREEN2),
    (datetime.date(2026, 10, 12), datetime.date(2026, 10, 15),
     "プライム感謝祭（予想）", BLUE),
    (datetime.date(2026, 11, 20), datetime.date(2026, 11, 22),
     "BF先行セール（予想）", LAVEND),
    (datetime.date(2026, 11, 23), datetime.date(2026, 11, 30),
     "ブラックフライデー（予想）", PURPLE),
]

WDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def retry(fn, *a, **k):
    from gspread.exceptions import APIError
    delay = 30
    for i in range(8):
        try:
            return fn(*a, **k)
        except APIError as e:
            if "429" in str(e) and i < 7:
                print(f"    [quota] {delay}s待機...", file=sys.stderr)
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


def categorize(name: str) -> str:
    n = str(name).replace("\n", "")
    if "感謝祭" in n:
        return "感謝祭"
    if "プライムデー" in n or "プライムセール" in n:
        return "プライム"
    if "スマイル" in n:
        return "スマイル"
    if "BF" in n or "ブラックフライデー" in n:
        return "BF"
    if "先行" in n:
        return "先行"
    if "タイムセール" in n:
        return "タイムセール"
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = Settings()
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = retry(gc.open_by_key, DEST_ID)
    ws = retry(sp.worksheet, args.tab)
    today = datetime.date.today()

    blocks = oshima_tab_blocks_config.get_blocks(args.tab)
    print(f"[{args.tab}] {len(blocks)}ブロック")

    # ========== 0) 現状確認 ==========
    col_a = retry(ws.col_values, 1)

    def a(row):
        return col_a[row - 1].strip() if row - 1 < len(col_a) else ""

    # ブロック構造検証 (元の行番号)
    for blk in blocks:
        F = blk["sales_forecast_row"]      # amazon販売予想
        E = F - 3                          # amazonイベント
        G = blk["forecast_row"]            # FBA在庫予想
        assert a(E) == "amazonイベント", f"{blk['code']}: R{E}={a(E)!r}"
        assert a(E + 1) == "amazonイベント係数", f"{blk['code']}: R{E+1}"
        assert a(F) == "amazon販売予想", f"{blk['code']}: R{F}"
        assert a(G) == "FBA在庫予想", f"{blk['code']}: R{G}"
        assert a(G + 1) == "FBA在庫実績", f"{blk['code']}: R{G+1}"
    already = a(blocks[0]["sales_forecast_row"] - 2) == "アマゾンイベント長沼"
    print(f"構造検証OK / 挿入済み: {already}")
    if args.dry_run:
        print("[dry-run] ここで終了")
        return

    # ========== 1) 日付軸延長 ==========
    row1_raw = retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    serials = [int(v) for v in row1 if isinstance(v, (int, float))]
    last_date = BASE + datetime.timedelta(days=max(serials))
    last_col = 2 + len(serials)
    if last_date < TARGET_END:
        n_new = (TARGET_END - last_date).days
        new_last = last_col + n_new
        if new_last > ws.col_count:
            retry(ws.add_cols, new_last - ws.col_count + 5)
        hdr, wd = [], []
        for i in range(1, n_new + 1):
            d = last_date + datetime.timedelta(days=i)
            hdr.append((d - BASE).days)
            wd.append(WDAYS[d.weekday()])
        retry(ws.update,
              range_name=f"{col_letter(last_col+1)}1:{col_letter(new_last)}1",
              values=[hdr], value_input_option='RAW')
        retry(ws.update,
              range_name=f"{col_letter(last_col+1)}3:{col_letter(new_last)}3",
              values=[wd])
        retry(sp.batch_update, {"requests": [{"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": last_col, "endColumnIndex": new_last},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "DATE", "pattern": "M/d"}}},
            "fields": "userEnteredFormat.numberFormat"}}]})
        print(f"日付軸延長: {last_date} → {TARGET_END} (+{n_new}列)")
        row1_raw = retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
        row1 = row1_raw[0] if row1_raw else []

    col_to_date = {i: BASE + datetime.timedelta(days=int(v))
                   for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    date_to_col = {d: c for c, d in col_to_date.items()}
    date_cols = set(col_to_date)
    min_c, max_c = min(date_cols), max(date_cols)
    END = col_letter(max_c)
    print(f"日付列: C〜{END}")

    # ========== 2) 行挿入 (下から上へ) ==========
    if not already:
        ins = []  # (startIndex_0based, count)
        for blk in blocks:
            F = blk["sales_forecast_row"]
            E = F - 3
            K = E + 1
            G = blk["forecast_row"]
            ins += [(G, 2), (F, 2), (K, 1), (E, 3)]
        ins.sort(reverse=True)
        reqs = [{"insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": s0, "endIndex": s0 + n},
            "inheritFromBefore": True}} for s0, n in ins]
        for i in range(0, len(reqs), 60):
            retry(sp.batch_update, {"requests": reqs[i:i+60]})
        print(f"行挿入完了: {len(blocks)}ブロック × 8行")

    # ========== 3) 新しい行番号の計算 ==========
    def remap(row: int, E: int, K: int, F: int, G: int, S: int) -> int:
        if row <= E:
            return row + S
        if row <= K:
            return row + S + 3
        if row <= F:
            return row + S + 4
        if row <= G:
            return row + S + 6
        return row + S + 8

    nb = []  # 新行番号入りブロック情報
    for i, blk in enumerate(blocks):
        S = 8 * i
        F = blk["sales_forecast_row"]
        E, K, G = F - 3, F - 2, blk["forecast_row"]
        d = dict(blk)
        for key in list(d.keys()):
            if key.endswith("_row"):
                d[key] = remap(d[key], E, K, F, G, S)
        d["_E"] = E + S                  # amazonイベント
        d["_price"] = E + S - 2          # セール価格
        d["_nag"] = E + S + 1            # アマゾンイベント長沼
        d["_avg7"] = E + S + 2
        d["_avg30"] = E + S + 3
        d["_coef"] = E + S + 5           # 係数長沼
        d["_f7"] = E + S + 8             # 長沼予想7日
        d["_f30"] = E + S + 9
        d["_inv_fc"] = G + S + 6         # FBA在庫予想
        d["_i7"] = G + S + 7
        d["_i30"] = G + S + 8
        d["_act"] = G + S + 9            # FBA在庫実績
        nb.append(d)

    # ラベル書き込み (1回のbatch)
    label_data = []
    for d in nb:
        label_data += [
            {"range": f"A{d['_nag']}", "values": [["アマゾンイベント長沼"]]},
            {"range": f"A{d['_avg7']}", "values": [["直近セール以外加重平均"]]},
            {"range": f"A{d['_avg30']}", "values": [["過去30日セール以外平均"]]},
            {"range": f"A{d['_coef']}", "values": [["アマゾンイベント係数長沼"]]},
            {"range": f"A{d['_f7']}", "values": [["アマゾン長沼予想７日"]]},
            {"range": f"A{d['_f30']}", "values": [["アマゾン長沼予想３０日"]]},
            {"range": f"A{d['_i7']}", "values": [["在庫予想長沼７日"]]},
            {"range": f"A{d['_i30']}", "values": [["在庫予想長沼３０日"]]},
        ]
    retry(ws.batch_update, label_data, value_input_option='USER_ENTERED')
    print("ラベル書き込み完了")

    # 検証
    col_a = retry(ws.col_values, 1)
    for d in nb:
        got = col_a[d["_nag"] - 1].strip() if d["_nag"] - 1 < len(col_a) else ""
        assert got == "アマゾンイベント長沼", f"{d['code']}: R{d['_nag']}={got!r}"
        got2 = col_a[d["sales_row"] - 1].strip() if d["sales_row"] - 1 < len(col_a) else ""
        assert got2 == "amazonFBA販売実績", f"{d['code']}: R{d['sales_row']}={got2!r}"
    print("行番号検証OK")

    # ========== 4) 元シートから実績読み込み ==========
    src = retry(gc.open_by_key, SRC_ID)

    def load_src(sheet, key_fn):
        wsx = retry(src.worksheet, sheet)
        vals = retry(wsx.get_all_values)
        hdr = vals[0]
        di = {}
        for i, v in enumerate(hdr):
            if v.count("-") == 2:
                try:
                    di[datetime.datetime.strptime(v, "%Y-%m-%d").date()] = i
                except ValueError:
                    pass
        out: Dict[Tuple[str, datetime.date], int] = {}
        for row in vals[1:]:
            if len(row) < 2 or not row[0]:
                continue
            k = key_fn(row[0])
            for dte, i in di.items():
                v = row[i] if i < len(row) else ""
                if v:
                    try:
                        out[(k, dte)] = out.get((k, dte), 0) + int(v)
                    except ValueError:
                        pass
        return out, sorted(di)

    def norm(sk):
        sx = sk.lower().strip().replace("（", "(").replace("）", ")")
        sx = re.sub(r"\([^)]*\)", "", sx)
        if ":" in sx:
            sx = sx.split(":", 1)[1]
        return sx.strip()

    amz_sales, amz_dates = load_src("日次Amazon販売推移", lambda x: x)
    rak_sales, rak_dates = load_src("日次楽天販売推移", norm)
    inv_data, inv_dates = load_src("日次Amazon在庫推移", lambda x: x)
    print(f"元データ: Amazon販売{len(amz_dates)}日 楽天{len(rak_dates)}日 在庫{len(inv_dates)}日")

    # ========== 5) 販売実績の書き込み + 明日以降空白 ==========
    sales_writes = []
    for d in nb:
        asin = d["asin"]
        code_n = norm(d["code"])
        # Amazon
        vals = []
        for dte in amz_dates:
            c = date_to_col.get(dte)
            if c:
                vals.append((c, amz_sales.get((asin, dte), 0)))
        if vals:
            c_lo, c_hi = vals[0][0], vals[-1][0]
            arr = [""] * (c_hi - c_lo + 1)
            for c, v in vals:
                arr[c - c_lo] = v
            sales_writes.append({
                "range": f"{col_letter(c_lo)}{d['sales_row']}:{col_letter(c_hi)}{d['sales_row']}",
                "values": [arr]})
        # 楽天
        if "rakuten_sales_row" in d:
            vals = []
            for dte in rak_dates:
                c = date_to_col.get(dte)
                if c:
                    vals.append((c, rak_sales.get((code_n, dte), 0)))
            if vals:
                c_lo, c_hi = vals[0][0], vals[-1][0]
                arr = [""] * (c_hi - c_lo + 1)
                for c, v in vals:
                    arr[c - c_lo] = v
                sales_writes.append({
                    "range": f"{col_letter(c_lo)}{d['rakuten_sales_row']}:{col_letter(c_hi)}{d['rakuten_sales_row']}",
                    "values": [arr]})
    retry(ws.batch_update, sales_writes, value_input_option='USER_ENTERED')
    print(f"販売実績書き込み: {len(sales_writes)}行分")

    # 明日以降空白 (Amazon/楽天/Yahoo実績行)
    fut_cols = [c for c, dte in col_to_date.items() if dte > today]
    if fut_cols:
        f_lo, f_hi = min(fut_cols), max(fut_cols)
        blank = [[""] * (f_hi - f_lo + 1)]
        blank_writes = []
        for d in nb:
            for key in ("sales_row", "rakuten_sales_row", "yahoo_sales_row"):
                if key in d:
                    blank_writes.append({
                        "range": f"{col_letter(f_lo)}{d[key]}:{col_letter(f_hi)}{d[key]}",
                        "values": blank})
        for i in range(0, len(blank_writes), 40):
            retry(ws.batch_update, blank_writes[i:i+40],
                  value_input_option='USER_ENTERED')
        print(f"明日以降を空白化: {len(blank_writes)}行")

    # ========== 6) FBA在庫実績の補完 ==========
    inv_rows_read = retry(ws.batch_get,
                          [f"C{d['_act']}:{END}{d['_act']}" for d in nb],
                          value_render_option='UNFORMATTED_VALUE')
    inv_writes = []
    for d, got in zip(nb, inv_rows_read):
        row_vals = got[0] if got else []
        for dte in inv_dates:
            if dte > today - datetime.timedelta(days=1):
                continue
            c = date_to_col.get(dte)
            if not c:
                continue
            cur = row_vals[c - 3] if c - 3 < len(row_vals) else ""
            cur_n = cur if isinstance(cur, (int, float)) else 0
            src_v = inv_data.get((d["asin"], dte), 0)
            if (not cur_n) and src_v > 0:
                inv_writes.append({"range": f"{col_letter(c)}{d['_act']}",
                                   "values": [[src_v]]})
    for i in range(0, len(inv_writes), 100):
        retry(ws.batch_update, inv_writes[i:i+100],
              value_input_option='USER_ENTERED')
    print(f"在庫実績補完: {len(inv_writes)}セル")

    # ========== 7) 長沼イベント行の構築 ==========
    # イベント行(E)とセール価格行(price)の書式・値を取得
    fmt_ranges = []
    for d in nb:
        fmt_ranges.append(f"'{args.tab}'!A{d['_price']}:{END}{d['_price']}")
        fmt_ranges.append(f"'{args.tab}'!A{d['_E']}:{END}{d['_E']}")
    grid = {}
    for i in range(0, len(fmt_ranges), 8):
        meta = retry(sp.fetch_sheet_metadata, params={
            "includeGridData": "true",
            "ranges": fmt_ranges[i:i+8],
            "fields": "sheets(data(startRow,rowData(values(formattedValue,effectiveFormat.backgroundColor))))",
        })
        for dblock in meta["sheets"][0]["data"]:
            r0 = dblock.get("startRow", 0) + 1
            grid[r0] = dblock.get("rowData", [{}])[0].get("values", [])

    def bg(cells, c):
        if c - 1 >= len(cells):
            return None
        col = cells[c-1].get("effectiveFormat", {}).get("backgroundColor")
        if not col:
            return None
        r = round(col.get("red", 0), 2)
        g = round(col.get("green", 0), 2)
        b = round(col.get("blue", 0), 2)
        if r > 0.95 and g > 0.95 and b > 0.95:
            return None
        return (r, g, b)

    def txt(cells, c):
        if c - 1 >= len(cells):
            return ""
        return (cells[c-1].get("formattedValue") or "").strip()

    all_fmt_reqs = []
    nag_value_writes = []
    block_periods: Dict[str, list] = {}
    for d in nb:
        pr = grid.get(d["_price"], [])
        ev = grid.get(d["_E"], [])
        past_cols = sorted(c for c, dte in col_to_date.items() if dte <= today)
        label_of = {}
        color_of = {}
        cur_label, prev_c = None, None
        for c in past_cols:
            t6, b6 = txt(ev, c), bg(ev, c)
            g4 = bg(pr, c) == PURE_GREEN
            if not (t6 or b6 or g4):
                cur_label, prev_c = None, c
                continue
            contiguous = (prev_c == c - 1) and cur_label
            if t6:
                cur_label = t6
            elif not contiguous:
                cur_label = "タイムセール"
            label_of[c] = cur_label
            color_of[c] = b6 if b6 else PURE_GREEN
            prev_c = c
        # 期間まとめ
        periods = []
        for c in sorted(label_of):
            if periods and periods[-1][1] == c - 1 and label_of[c] == periods[-1][2]:
                periods[-1][1] = c
            else:
                periods.append([c, c, label_of[c], color_of[c], False])
        # 未来セール
        for d0, d1, lab, colr in FUTURE_SALES:
            c0, c1 = date_to_col.get(d0), date_to_col.get(d1)
            if c0 and c1:
                periods.append([c0, c1, lab,
                                (colr["red"], colr["green"], colr["blue"]), True])
        block_periods[d["code"]] = periods
        # 値行
        arr = [""] * (max_c - min_c + 1)
        for c0, c1, lab, colr, fut in periods:
            for c in range(c0, c1 + 1):
                arr[c - min_c] = lab
        nag_value_writes.append({
            "range": f"{col_letter(min_c)}{d['_nag']}:{END}{d['_nag']}",
            "values": [arr]})
        # 書式
        r = d["_nag"]
        all_fmt_reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": r-1, "endRowIndex": r,
                      "startColumnIndex": min_c-1, "endColumnIndex": max_c},
            "cell": {"userEnteredFormat": {
                "backgroundColor": WHITE,
                "textFormat": {"foregroundColor": BLACK}}},
            "fields": ("userEnteredFormat.backgroundColor,"
                       "userEnteredFormat.textFormat.foregroundColor")}})
        for c0, c1, lab, colr, fut in periods:
            cd = {"red": colr[0], "green": colr[1], "blue": colr[2]}
            all_fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1, "endRowIndex": r,
                          "startColumnIndex": c0-1, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": cd, "textFormat": {"foregroundColor": cd}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})
            all_fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1, "endRowIndex": r,
                          "startColumnIndex": c0-1, "endColumnIndex": c0},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": cd, "textFormat": {"foregroundColor": BLACK}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})

    for i in range(0, len(nag_value_writes), 10):
        retry(ws.batch_update, nag_value_writes[i:i+10],
              value_input_option='USER_ENTERED')
    for i in range(0, len(all_fmt_reqs), 80):
        retry(sp.batch_update, {"requests": all_fmt_reqs[i:i+80]})
    print(f"長沼イベント行構築完了 ({sum(len(v) for v in block_periods.values())}期間)")

    # ========== 8) 係数長沼 (過去=実測 / 未来=直近5回平均) ==========
    # 販売実績を読み直し (係数計算のベースライン用)
    sales_read = retry(ws.batch_get,
                       [f"C{d['sales_row']}:{END}{d['sales_row']}" for d in nb],
                       value_render_option='UNFORMATTED_VALUE')
    coef_writes = []
    for d, got in zip(nb, sales_read):
        srow = got[0] if got else []

        def sales_at(c):
            v = srow[c - 3] if c - 3 < len(srow) else ""
            return float(v) if isinstance(v, (int, float)) else None

        periods = block_periods[d["code"]]
        sale_days = set()
        for c0, c1, lab, colr, fut in periods:
            sale_days.update(range(c0, c1 + 1))

        def baseline(c_start):
            picked = []
            c = c_start - 1
            while c >= min_c and len(picked) < 7:
                dte = col_to_date.get(c)
                if dte and dte.weekday() < 5 and c not in sale_days:
                    v = sales_at(c)
                    if v is not None and v > 0:
                        picked.append(v)
                c -= 1
            return statistics.mean(picked) if picked else None

        hist = []  # (end_col, category, coef)
        past_coef = {}
        for c0, c1, lab, colr, fut in sorted(periods):
            if fut:
                continue
            svals = [sales_at(c) for c in range(c0, c1 + 1)]
            svals = [v for v in svals if v is not None and v > 0]
            base = baseline(c0)
            if not svals or not base:
                continue
            coef = round(statistics.mean(svals) / base, 2)
            past_coef[(c0, c1)] = coef
            hist.append((c1, categorize(lab), coef))
        hist.sort()

        def predict(cat):
            hs = [cf for _, ct, cf in hist if ct == cat][-5:]
            if hs:
                return round(statistics.mean(hs), 2)
            hs = [cf for _, _, cf in hist][-5:]
            if hs:
                return round(statistics.mean(hs), 2)
            return 1

        arr = []
        for c in range(min_c, max_c + 1):
            if c not in date_cols:
                arr.append("")
                continue
            val = 1
            for c0, c1, lab, colr, fut in periods:
                if c0 <= c <= c1:
                    if fut:
                        val = predict(categorize(lab))
                    else:
                        val = past_coef.get((c0, c1), 1)
                    break
            arr.append(val)
        coef_writes.append({
            "range": f"{col_letter(min_c)}{d['_coef']}:{END}{d['_coef']}",
            "values": [arr]})
    for i in range(0, len(coef_writes), 10):
        retry(ws.batch_update, coef_writes[i:i+10],
              value_input_option='USER_ENTERED')
    print("係数長沼行 構築完了")

    # ========== 9) 数式行 ==========
    for d in nb:
        S = d["sales_row"]
        E = d["_E"]
        N = d["_nag"]
        A7, A30 = d["_avg7"], d["_avg30"]
        CF = d["_coef"]
        F7, F30 = d["_f7"], d["_f30"]
        I7, I30 = d["_i7"], d["_i30"]
        ACT = d["_act"]
        CHG, FR, TO = d["change_qty_row"], d["from_row"], d["to_row"]

        def wd7(ref):
            # 直近10日(セール以外・全曜日)の指数加重平均 (α=0.35)。
            # バックテスト(2026-07-25, マウスピース7商品×2476日)で
            # 直近7平日平均(誤差5.76)より加重10日(5.26)が最良だったため採用。
            conds = (f'$C$1:$1<{ref}, '
                     f'$C${E}:${E}="", $C${N}:${N}="", '
                     f'$C${S}:${S}<>""')
            weights = ("{0.35;0.2275;0.147875;0.09611875;0.0624771875;"
                       "0.040610171875;0.02639661171875;0.0171577976171875;"
                       "0.0111525684511719;0.0072491694932617}")
            vals = (f'ARRAY_CONSTRAIN(SORT('
                    f'TRANSPOSE(FILTER($C${S}:${S}, {conds})), '
                    f'TRANSPOSE(FILTER($C$1:$1, {conds})), '
                    f'FALSE), 10, 1)')
            return (f'ROUND(SUMPRODUCT({vals}, {weights})/0.9865372085, 1)')

        def avg_e(ref, days):
            return (f'AVERAGEIFS($C${S}:${S}, '
                    f'$C$1:$1, ">="&{ref}-{days}, $C$1:$1, "<"&{ref}, '
                    f'$C${E}:${E}, "", $C${N}:${N}, "")')

        LASTACT = (f'LOOKUP(2, ARRAYFORMULA(1/(($C${ACT}:${ACT}<>"")'
                   f'*($C${ACT}:${ACT}<>0)*($C$1:$1<=TODAY()))), '
                   f'$C${ACT}:${ACT})')

        b7 = f'=IFERROR({wd7("TODAY()")}, "")'
        e30 = '""'
        for days in [365, 90, 30]:
            e30 = f'IFERROR(ROUND({avg_e("TODAY()", days)}, 1), {e30})'
        b30 = f'={e30}'
        retry(ws.batch_update, [
            {"range": f"B{A7}", "values": [[b7]]},
            {"range": f"B{A30}", "values": [[b30]]},
        ], value_input_option='USER_ENTERED')

        r_a7, r_a30, r_f7, r_f30, r_i7, r_i30 = [], [], [], [], [], []
        for c in range(min_c, max_c + 1):
            if c not in date_cols:
                for lst in (r_a7, r_a30, r_f7, r_f30, r_i7, r_i30):
                    lst.append("")
                continue
            L = col_letter(c)
            P = col_letter(c - 1)
            r_a7.append(f'=IF({L}$1>TODAY(), $B${A7}, '
                        f'IFERROR({wd7(f"{L}$1")}, $B${A7}))')
            ex = f"$B${A30}"
            for days in [365, 90, 30]:
                ex = f'IFERROR(ROUND({avg_e(f"{L}$1", days)}, 1), {ex})'
            r_a30.append(f'=IF({L}$1>TODAY(), $B${A30}, {ex})')
            r_f7.append(f'=IFERROR(ROUND({L}{A7}*{L}{CF}, 1), "")')
            r_f30.append(f'=IFERROR(ROUND({L}{A30}*{L}{CF}, 1), "")')
            seed7 = f'IF({P}$1<=TODAY(), {LASTACT}, {P}{I7})'
            seed30 = f'IF({P}$1<=TODAY(), {LASTACT}, {P}{I30})'
            ch7 = (f'{seed7}+IF({P}{TO}="FBA",{P}{CHG},0)-{P}{F7}'
                   f'-IF({P}{FR}="FBA",{P}{CHG},0)')
            ch30 = (f'{seed30}+IF({P}{TO}="FBA",{P}{CHG},0)-{P}{F30}'
                    f'-IF({P}{FR}="FBA",{P}{CHG},0)')
            past = f'IF(OR({L}{ACT}="",{L}{ACT}=0), "", {L}{ACT})'
            r_i7.append(f'=IF({L}$1<=TODAY(), {past}, ROUND({ch7}, 1))')
            r_i30.append(f'=IF({L}$1<=TODAY(), {past}, ROUND({ch30}, 1))')

        payload = [
            {"range": f"{col_letter(min_c)}{A7}:{END}{A7}", "values": [r_a7]},
            {"range": f"{col_letter(min_c)}{A30}:{END}{A30}", "values": [r_a30]},
            {"range": f"{col_letter(min_c)}{F7}:{END}{F7}", "values": [r_f7]},
            {"range": f"{col_letter(min_c)}{F30}:{END}{F30}", "values": [r_f30]},
            {"range": f"{col_letter(min_c)}{I7}:{END}{I7}", "values": [r_i7]},
            {"range": f"{col_letter(min_c)}{I30}:{END}{I30}", "values": [r_i30]},
        ]
        for p in payload:
            retry(ws.batch_update, [p], value_input_option='USER_ENTERED')
        print(f"  数式行 {d['code']} 完了")

    # ========== 10) config 更新 ==========
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "oshima_tab_blocks_config.py")
    src_txt = open(cfg_path, encoding="utf-8").read()
    lines = []
    lines.append(f'    # {datetime.date.today()}: 長沼行8行/ブロック挿入済み (oshima_naganuma_upgrade.py)')
    lines.append(f'    "{args.tab}": [')
    for d in nb:
        keys = [k for k in d if not k.startswith("_")]
        parts = []
        for k in ("code", "asin"):
            parts.append(f'"{k}": "{d[k]}"')
        for k in keys:
            if k in ("code", "asin"):
                continue
            parts.append(f'"{k}": {d[k]}')
        lines.append("        {" + ", ".join(parts) + "},")
    lines.append("    ],")
    new_section = "\n".join(lines)
    pat = re.compile(
        r'(?:^[ \t]*#[^\n]*\n)*^[ \t]*"' + re.escape(args.tab) + r'": \[.*?^\s*\],',
        re.S | re.M)
    if not pat.search(src_txt):
        print("⚠ config 内にタブ定義が見つからず更新スキップ", file=sys.stderr)
    else:
        open(cfg_path, "w", encoding="utf-8").write(pat.sub(lambda m: new_section, src_txt, count=1))
        print("oshima_tab_blocks_config.py 更新完了")

    print("\n✅ アップグレード完了")


if __name__ == "__main__":
    main()
