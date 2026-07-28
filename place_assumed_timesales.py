#!/usr/bin/env python3
"""過去のタイムセール周期から将来のタイムセールを「タイムセール仮」として仮置きする。

対象商品: 過去のタイムセールが4回以上 かつ 直近60日以内に実施がある商品
配置ルール:
  間隔 = 過去の平均間隔 (終了→次回開始、3〜60日にクランプ)
  期間 = 過去の期間の中央値 (1〜14日)
  係数 = 直近5回のタイムセール係数平均
  直近の実タイムセール終了日から周期的に 2027-03-31 まで配置
  既存イベントと重なる回はスキップ (周期は維持)
再実行時は既存の「タイムセール仮」を全て消してから引き直す
(実予定が入るほど仮置きが実データに置き換わっていく)

使い方:
  python3 place_assumed_timesales.py --tab "マウスピース(在庫)"
  python3 place_assumed_timesales.py --all
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
LABEL = "タイムセール仮"
PALE = {"red": 0.85, "green": 0.93, "blue": 0.85}   # 仮置き (実セールより薄い緑)
BLACK = {"red": 0, "green": 0, "blue": 0}
WHITE = {"red": 1, "green": 1, "blue": 1}
MIN_PERIODS = 4
RECENT_DAYS = 60
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


def process_tab(sp, tab, records):
    ws = retry(sp.worksheet, tab)
    today = datetime.date.today()
    horizon = datetime.date(2027, 3, 31)
    row1 = (retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
            or [[]])[0]
    c2d = {i: BASE + datetime.timedelta(days=int(v))
           for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    d2c = {d: c for c, d in c2d.items()}
    max_c = max(c2d)
    END = col_letter(max_c)
    col_a = retry(ws.col_values, 1)
    blocks = oshima_tab_blocks_config.get_blocks(tab)
    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    print(f"[{tab}] {len(blocks)}ブロック")

    for bi, b in enumerate(blocks):
        lo, hi = bounds[bi], bounds[bi + 1]
        ev = cf = None
        for r in range(lo, hi):
            v = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
            if v == "アマゾンイベント" and ev is None:
                ev = r
            elif v == "アマゾンイベント係数" and cf is None:
                cf = r
        if not ev or not cf:
            continue
        got = retry(ws.batch_get, [f"C{ev}:{END}{ev}", f"C{cf}:{END}{cf}"],
                    value_render_option='UNFORMATTED_VALUE')
        evr = got[0][0] if got[0] else []
        cfr = got[1][0] if got[1] else []
        lab, coefs = {}, {}
        for i, v in enumerate(evr):
            d = c2d.get(i + 3)
            if d and isinstance(v, str) and v.strip():
                lab[d] = v.strip()
        for i, v in enumerate(cfr):
            d = c2d.get(i + 3)
            if d and isinstance(v, (int, float)):
                coefs[d] = float(v)

        # 1) 既存の仮置きを撤去
        old = sorted(d for d, v in lab.items() if "仮" in v)
        reqs = []
        if old:
            values = []
            for d in old:
                c = d2c[d]
                values.append({"range": f"{col_letter(c)}{ev}",
                               "values": [[""]]})
                values.append({"range": f"{col_letter(c)}{cf}",
                               "values": [[1]]})
                reqs.append({"repeatCell": {
                    "range": {"sheetId": ws.id, "startRowIndex": ev - 1,
                              "endRowIndex": ev, "startColumnIndex": c - 1,
                              "endColumnIndex": c},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": WHITE,
                        "textFormat": {"foregroundColor": BLACK}}},
                    "fields": ("userEnteredFormat(backgroundColor,"
                               "textFormat.foregroundColor)")}})
            for i in range(0, len(values), 100):
                retry(ws.batch_update, [dict(x) for x in values[i:i + 100]],
                      value_input_option='USER_ENTERED')
            for d in old:
                del lab[d]

        # 2) 過去の実タイムセール周期を分析
        past = {d: v for d, v in lab.items()
                if d <= today and "タイムセール" in v and "祭" not in v}
        periods, cur = [], None
        for d in sorted(past):
            if cur and (d - cur[1]).days == 1:
                cur[1] = d
            else:
                cur = [d, d]
                periods.append(cur)
        if len(periods) < MIN_PERIODS:
            continue
        last_end = periods[-1][1]
        if (today - last_end).days > RECENT_DAYS:
            continue
        gaps = [(periods[i][0] - periods[i - 1][1]).days
                for i in range(1, len(periods))]
        gap = max(3, min(60, round(statistics.mean(gaps))))
        dur = max(1, min(14, round(statistics.median(
            (p1 - p0).days + 1 for p0, p1 in periods))))
        cvals = []
        for p0, p1 in periods[-5:]:
            vs = [coefs[d] for d in coefs if p0 <= d <= p1]
            if vs:
                cvals.append(max(vs))
        coef = round(statistics.mean(cvals), 2) if cvals else 1.2

        # 3) 周期配置 (既存イベントと重なる回はスキップ)
        event_days = set(lab)
        placed = []
        start = last_end + datetime.timedelta(days=gap)
        while start <= horizon:
            end = start + datetime.timedelta(days=dur - 1)
            span = [start + datetime.timedelta(days=i)
                    for i in range((end - start).days + 1)]
            if (start > today and all(d in d2c for d in span)
                    and not any(d in event_days for d in span)):
                placed.append((start, end))
                event_days.update(span)
            start = end + datetime.timedelta(days=gap)

        # 4) 書き込み
        values = []
        for p0, p1 in placed:
            c0, c1 = d2c[p0], d2c[p1]
            n = c1 - c0 + 1
            values.append({"range": f"{col_letter(c0)}{ev}:{col_letter(c1)}{ev}",
                           "values": [[LABEL] * n]})
            values.append({"range": f"{col_letter(c0)}{cf}:{col_letter(c1)}{cf}",
                           "values": [[coef] * n]})
            reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": ev - 1,
                          "endRowIndex": ev, "startColumnIndex": c0 - 1,
                          "endColumnIndex": c1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": PALE,
                    "textFormat": {"foregroundColor": PALE}}},
                "fields": ("userEnteredFormat(backgroundColor,"
                           "textFormat.foregroundColor)")}})
            reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": ev - 1,
                          "endRowIndex": ev, "startColumnIndex": c0 - 1,
                          "endColumnIndex": c0},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": PALE,
                    "textFormat": {"foregroundColor": BLACK}}},
                "fields": ("userEnteredFormat(backgroundColor,"
                           "textFormat.foregroundColor)")}})
        for i in range(0, len(values), 100):
            retry(ws.batch_update, [dict(x) for x in values[i:i + 100]],
                  value_input_option='USER_ENTERED')
        for i in range(0, len(reqs), 60):
            retry(sp.batch_update, {"requests": reqs[i:i + 60]})
        print(f"  {b['code']}: 撤去{len(old)}日 / 仮置き{len(placed)}回 "
              f"(間隔{gap}日, 期間{dur}日, 係数{coef})", flush=True)
        for p0, p1 in placed:
            records.append([tab.replace("(在庫)", "").strip(), b["code"],
                            p0.strftime("%Y/%m/%d"), p1.strftime("%Y/%m/%d"),
                            (p1 - p0).days + 1, coef, gap])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    settings = Settings()
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = retry(gc.open_by_key, DEST_ID)
    tabs = TABS if args.all else [args.tab]
    records = []
    for tab in tabs:
        process_tab(sp, tab, records)

    # 一覧シートに掲載 (全タブ実行時のみ全面更新)
    if args.all:
        title = "タイムセール仮置き一覧"
        try:
            wsl = retry(sp.worksheet, title)
        except Exception:
            idx = next((i + 1 for i, w in enumerate(sp.worksheets())
                        if w.title == "タイムセール入力"), None)
            wsl = retry(sp.add_worksheet, title=title,
                        rows=max(50, len(records) + 10), cols=8, index=idx)
        retry(wsl.clear)
        today = datetime.date.today().strftime("%Y/%m/%d")
        hdr = [[f"タイムセール仮置き一覧 (過去の周期から自動配置 / 更新: {today})",
                "", "", "", "", "", ""],
               ["タブ", "商品", "開始日", "終了日", "日数", "係数", "間隔(日)"]]
        data = hdr + records
        if wsl.row_count < len(data) + 5:
            retry(wsl.resize, rows=len(data) + 10)
        retry(wsl.update, range_name=f"A1:G{len(data)}", values=data,
              value_input_option='USER_ENTERED')
        retry(sp.batch_update, {"requests": [
            {"repeatCell": {
                "range": {"sheetId": wsl.id, "startRowIndex": 1,
                          "endRowIndex": 2},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.85, "green": 0.93,
                                        "blue": 0.85},
                    "textFormat": {"bold": True}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            {"updateSheetProperties": {
                "properties": {"sheetId": wsl.id,
                               "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount"}},
        ]})
        print(f"一覧シート更新: {len(records)}件")
    print("✅ 仮置き完了")


if __name__ == "__main__":
    main()
