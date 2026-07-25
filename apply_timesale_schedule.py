#!/usr/bin/env python3
"""「タイムセール入力」シートの予定を全商品タブのイベント行へ反映する。

入力シート (タイムセール入力) の3行目以降:
  A: 商品コード (oshima_tab_blocks_config の code、例 MP-02MHD / DS-01)
  B: 開始日 (例 2026/8/1)
  C: 終了日 (例 2026/8/7)
  D: セール名 (空欄なら「タイムセール」)
  E: 係数 (空欄なら直近5回のタイムセール係数平均を自動計算)
  F: 反映結果 (スクリプトが書き込む)

反映内容 (対象ブロックの各行、開始日〜終了日):
  アマゾンイベント長沼 行: ラベル + 色 (先頭セルのみ黒文字、以降は隠し文字)
  アマゾンイベント係数長沼 行: 係数値

安全策: 過去日は変更しない。既にイベントがある日はスキップして報告。

使い方:
  python3 apply_timesale_schedule.py [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
import oshima_tab_blocks_config

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)
INPUT_SHEET = "タイムセール入力"
GREEN = {"red": 0.72, "green": 0.88, "blue": 0.72}
BLACK = {"red": 0, "green": 0, "blue": 0}
TABS = ["マウスピース(在庫)", "DS-01 (在庫) ", "TG-01(在庫)", "TG-02(在庫)",
        "GC-01(在庫)", "GC-02(在庫)", "PCI-01", "WB-01(在庫)", "WB-02",
        "TS-01", "PG-01"]


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


def parse_date(v) -> datetime.date | None:
    if isinstance(v, (int, float)):
        return BASE + datetime.timedelta(days=int(v))
    t = str(v).strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d"):
        try:
            d = datetime.datetime.strptime(t, fmt).date()
            if fmt == "%m/%d":
                d = d.replace(year=datetime.date.today().year)
            return d
        except ValueError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
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
    today = datetime.date.today()

    # 入力シート (無ければ作成)
    try:
        wsi = retry(sp.worksheet, INPUT_SHEET)
    except Exception:
        idx = next((i + 1 for i, w in enumerate(sp.worksheets())
                    if w.title == "タイムセール頻度"), None)
        wsi = retry(sp.add_worksheet, title=INPUT_SHEET, rows=50, cols=6,
                    index=idx)
        retry(wsi.update, range_name="A1:F2", values=[
            ["セラーセントラルでタイムセールを設定したらここに入力して "
             "apply_timesale_schedule.py を実行", "", "", "", "", ""],
            ["商品コード", "開始日", "終了日", "セール名(空欄=タイムセール)",
             "係数(空欄=自動)", "反映結果"],
        ], value_input_option='USER_ENTERED')
        retry(sp.batch_update, {"requests": [{"repeatCell": {
            "range": {"sheetId": wsi.id, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.95},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}}]})
        print(f"入力シート「{INPUT_SHEET}」を作成しました。3行目から入力してください")
        return

    rows = retry(wsi.get, "A3:F50")
    entries = []
    for i, row in enumerate(rows, 3):
        row = (row + [""] * 6)[:6]
        code = str(row[0]).strip()
        if not code:
            continue
        d0, d1 = parse_date(row[1]), parse_date(row[2])
        entries.append({"row": i, "code": code, "d0": d0, "d1": d1,
                        "name": str(row[3]).strip() or "タイムセール",
                        "coef": str(row[4]).strip()})
    if not entries:
        print("入力がありません (3行目以降に商品コード・開始日・終了日を入力)")
        return

    # code → (tab, block)
    code_map = {}
    for tab in TABS:
        for b in oshima_tab_blocks_config.get_blocks(tab):
            code_map[b["code"].lower()] = (tab, b)

    results = {}
    by_tab = {}
    for e in entries:
        key = e["code"].lower()
        if key not in code_map:
            results[e["row"]] = f"エラー: 商品コード不明 ({e['code']})"
            continue
        if not e["d0"] or not e["d1"] or e["d1"] < e["d0"]:
            results[e["row"]] = "エラー: 日付が不正"
            continue
        if e["d1"] < today:
            results[e["row"]] = "スキップ: 過去の期間"
            continue
        tab, b = code_map[key]
        by_tab.setdefault(tab, []).append((e, b))

    for tab, items in by_tab.items():
        ws = retry(sp.worksheet, tab)
        row1 = (retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
                or [[]])[0]
        d2c = {BASE + datetime.timedelta(days=int(v)): i
               for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
        col_a = retry(ws.col_values, 1)
        blocks = oshima_tab_blocks_config.get_blocks(tab)
        bounds = [bb["asin_row"] for bb in blocks] + [len(col_a) + 2]

        def block_rows(b):
            bi = next(i for i, bb in enumerate(blocks)
                      if bb["code"] == b["code"])
            lo, hi = bounds[bi], bounds[bi + 1]
            m = {}
            for r in range(lo, hi):
                v = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
                if v and v not in m:
                    m[v] = r
            return m

        for e, b in items:
            m = block_rows(b)
            ev = m.get("アマゾンイベント長沼")
            cf = m.get("アマゾンイベント係数長沼")
            if not ev or not cf:
                results[e["row"]] = "エラー: イベント行が見つからない"
                continue
            days = []
            d = max(e["d0"], today)
            while d <= e["d1"]:
                c = d2c.get(d)
                if c:
                    days.append((d, c))
                d += datetime.timedelta(days=1)
            if not days:
                results[e["row"]] = "エラー: 日付がシート範囲外"
                continue
            # 既存イベントの確認
            c_lo, c_hi = days[0][1], days[-1][1]
            cur = retry(ws.get,
                        f"{col_letter(c_lo)}{ev}:{col_letter(c_hi)}{ev}")
            cur_row = (cur[0] + [""] * len(days))[:len(days)] if cur else \
                [""] * len(days)
            occupied = [str(x).strip() for x in cur_row if str(x).strip()
                        and str(x).strip() != e["name"]]
            if occupied:
                results[e["row"]] = (f"スキップ: 既にイベントあり "
                                     f"({occupied[0][:12]}...)")
                continue
            # 係数: 指定 or 直近5回のタイムセール期間の係数平均
            if e["coef"]:
                coef = float(e["coef"])
            else:
                evr = retry(ws.get, f"C{ev}:{col_letter(max(d2c.values()))}{ev}",
                            value_render_option='UNFORMATTED_VALUE')
                cfr = retry(ws.get, f"C{cf}:{col_letter(max(d2c.values()))}{cf}",
                            value_render_option='UNFORMATTED_VALUE')
                evv = (evr[0] if evr else [])
                cfv = (cfr[0] if cfr else [])
                c2d = {c: dd for dd, c in d2c.items()}
                lab, coefs = {}, {}
                for i, v in enumerate(evv):
                    dd = c2d.get(i + 3)
                    if dd and dd <= today and isinstance(v, str) and \
                            "タイムセール" in v and "祭" not in v:
                        lab[dd] = v.strip()
                for i, v in enumerate(cfv):
                    dd = c2d.get(i + 3)
                    if dd and isinstance(v, (int, float)):
                        coefs[dd] = float(v)
                periods, curp = [], None
                for dd in sorted(lab):
                    if curp and (dd - curp[1]).days == 1:
                        curp[1] = dd
                    else:
                        curp = [dd, dd]
                        periods.append(curp)
                vals5 = []
                for p0, p1 in periods[-5:]:
                    vs = [coefs[dd] for dd in coefs if p0 <= dd <= p1]
                    if vs:
                        vals5.append(max(vs))
                coef = round(statistics.mean(vals5), 2) if vals5 else 1.2
            if args.dry_run:
                results[e["row"]] = (f"dry-run: {tab} R{ev} "
                                     f"{days[0][0]}〜{days[-1][0]} 係数{coef}")
                continue
            # 書き込み: ラベル + 係数
            retry(ws.update,
                  range_name=f"{col_letter(c_lo)}{ev}:{col_letter(c_hi)}{ev}",
                  values=[[e["name"]] * len(days)],
                  value_input_option='USER_ENTERED')
            retry(ws.update,
                  range_name=f"{col_letter(c_lo)}{cf}:{col_letter(c_hi)}{cf}",
                  values=[[coef] * len(days)],
                  value_input_option='USER_ENTERED')
            # 色: 先頭のみ黒文字、以降は隠し文字
            reqs = [{"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": ev - 1,
                          "endRowIndex": ev, "startColumnIndex": c_lo - 1,
                          "endColumnIndex": c_hi},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": GREEN,
                    "textFormat": {"foregroundColor": GREEN}}},
                "fields": ("userEnteredFormat(backgroundColor,"
                           "textFormat.foregroundColor)")}},
                {"repeatCell": {
                    "range": {"sheetId": ws.id, "startRowIndex": ev - 1,
                              "endRowIndex": ev, "startColumnIndex": c_lo - 1,
                              "endColumnIndex": c_lo},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": GREEN,
                        "textFormat": {"foregroundColor": BLACK}}},
                    "fields": ("userEnteredFormat(backgroundColor,"
                               "textFormat.foregroundColor)")}}]
            retry(sp.batch_update, {"requests": reqs})
            results[e["row"]] = (f"OK: {days[0][0]}〜{days[-1][0]} 係数{coef}")
            print(f"  {e['code']}: {results[e['row']]}", flush=True)

    # 結果をF列に書き戻し
    updates = [{"range": f"F{r}", "values": [[msg]]}
               for r, msg in sorted(results.items())]
    if updates and not args.dry_run:
        retry(wsi.batch_update, updates, value_input_option='USER_ENTERED')
    for r, msg in sorted(results.items()):
        print(f"行{r}: {msg}")
    print("✅ 反映完了")


if __name__ == "__main__":
    main()
