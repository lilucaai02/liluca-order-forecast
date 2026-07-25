#!/usr/bin/env python3
"""
Yahoo長沼構成を商品タブへ展開する (楽天と同構成)。

各ブロックの「Yahoo販売予想」の上に5行を挿入 (既存の予想・実績は保持):
  Yahooイベント長沼       (実績スパイク検出 + 5のつく日 + ゾロ目の日)
  直近7平日セール以外平均
  直近セール以外加重平均
  Yahooイベント係数長沼
  Yahoo販売予測長沼      (ハイブリッド: イベント日=平日7×係数 / 通常日=加重×係数)

イベント優先順位: 検出セール > 5のつく日(5,15,25) > ゾロ目の日(11,22)

使い方:
  python3 yahoo_naganuma_rollout.py --tab "マウスピース(在庫)" [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
import oshima_tab_blocks_config

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)
WEIGHTS = ("{0.35;0.2275;0.147875;0.09611875;0.0624771875;"
           "0.040610171875;0.02639661171875;0.0171577976171875;"
           "0.0111525684511719;0.0072491694932617}")
TEAL = {"red": 0.72, "green": 0.88, "blue": 0.8}      # 検出セール
YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.7}     # 5のつく日
LAVEND = {"red": 0.85, "green": 0.82, "blue": 0.91}   # ゾロ目の日
BLACK = {"red": 0, "green": 0, "blue": 0}
WHITE = {"red": 1, "green": 1, "blue": 1}


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


def is5day(d):
    return d.day in (5, 15, 25)


def iszoro(d):
    return d.day in (11, 22)


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
    blocks = oshima_tab_blocks_config.get_blocks(args.tab)
    today = datetime.date.today()

    row1_raw = retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    col_to_date = {i: BASE + datetime.timedelta(days=int(v))
                   for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    d_to_c = {d: c for c, d in col_to_date.items()}
    date_cols = set(col_to_date)
    min_c, max_c = min(date_cols), max(date_cols)
    END = col_letter(max_c)

    col_a = retry(ws.col_values, 1)

    def a(r):
        return col_a[r - 1].strip() if r - 1 < len(col_a) else ""

    blocks = [b for b in blocks if "yahoo_sales_forecast_row" in b
              and "yahoo_sales_row" in b]
    for b in blocks:
        fc, sl = b["yahoo_sales_forecast_row"], b["yahoo_sales_row"]
        assert a(fc) == "Yahoo販売予想", f"{b['code']}: R{fc}={a(fc)!r}"
        assert a(sl) == "Yahoo販売実績", f"{b['code']}: R{sl}={a(sl)!r}"
        assert sl == fc + 1, b["code"]
    already = a(blocks[0]["yahoo_sales_forecast_row"] - 1) == "Yahoo販売予測長沼"
    print(f"[{args.tab}] {len(blocks)}ブロック / 挿入済み: {already}")

    # ===== 実績読み込み =====
    sales_by_block = {}
    daily_total = {}
    got = retry(ws.batch_get,
                [f"C{b['yahoo_sales_row']}:{END}{b['yahoo_sales_row']}"
                 for b in blocks],
                value_render_option='UNFORMATTED_VALUE')
    for b, g in zip(blocks, got):
        row = g[0] if g else []
        m = {}
        for i, v in enumerate(row):
            c = i + 3
            d = col_to_date.get(c)
            if d and d <= today and isinstance(v, (int, float)):
                m[d] = float(v)
                daily_total[d] = daily_total.get(d, 0) + v
        sales_by_block[b["code"]] = m

    days = sorted(daily_total)
    med = statistics.median(daily_total[d] for d in days) if days else 0
    print(f"日次中央値: {med:.1f}")

    # ===== スパイク検出 =====
    events = []
    if med >= 2:
        th = med * 1.5
        run, gap = [], 0
        periods = []
        for d in days:
            if daily_total[d] >= th:
                run.append(d); gap = 0
            else:
                if run:
                    gap += 1
                    if gap > 1:
                        if len(run) >= 2:
                            periods.append([run[0], run[-1]])
                        run, gap = [], 0
        if len(run) >= 2:
            periods.append([run[0], run[-1]])
        events = [(d0, d1, "Yahooセール", False) for d0, d1 in periods]
        print(f"スパイク検出: {len(periods)}期間")
    else:
        print("低販売量 → スパイク検出スキップ (5のつく日/ゾロ目のみ)")

    event_days = set()
    for d0, d1, lab, fut in events:
        dd = d0
        while dd <= d1:
            event_days.add(dd)
            dd += datetime.timedelta(days=1)
    all_dates = sorted(col_to_date.values())
    five_days = [d for d in all_dates if is5day(d) and d not in event_days]
    zoro_days = [d for d in all_dates if iszoro(d) and d not in event_days
                 and not is5day(d)]

    if args.dry_run:
        for d0, d1, lab, fut in events:
            print(f"  {d0}〜{d1} {lab}")
        print(f"5のつく日: {len(five_days)}日 / ゾロ目: {len(zoro_days)}日")
        return

    # ===== 5行挿入 =====
    if not already:
        ins = sorted((b["yahoo_sales_forecast_row"] for b in blocks),
                     reverse=True)
        reqs = [{"insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": r - 1, "endIndex": r + 4},
            "inheritFromBefore": True}} for r in ins]
        for i in range(0, len(reqs), 60):
            retry(sp.batch_update, {"requests": reqs[i:i+60]})
        print("5行×ブロック 挿入完了")

    nb = []
    for k, b in enumerate(blocks):
        S = 5 * k
        fc = b["yahoo_sales_forecast_row"]
        d = dict(b)
        for key in list(d.keys()):
            if key.endswith("_row"):
                d[key] = d[key] + S + (5 if d[key] >= fc else 0)
        d["_yev"] = fc + S
        d["_ywd"] = fc + S + 1
        d["_ywv"] = fc + S + 2
        d["_ycoef"] = fc + S + 3
        d["_yhyb"] = fc + S + 4
        nb.append(d)

    labels = []
    for d in nb:
        labels += [
            {"range": f"A{d['_yev']}", "values": [["Yahooイベント長沼"]]},
            {"range": f"A{d['_ywd']}", "values": [["直近7平日セール以外平均"]]},
            {"range": f"A{d['_ywv']}", "values": [["直近セール以外加重平均"]]},
            {"range": f"A{d['_ycoef']}", "values": [["Yahooイベント係数長沼"]]},
            {"range": f"A{d['_yhyb']}", "values": [["Yahoo販売予測長沼"]]},
        ]
    retry(ws.batch_update, labels, value_input_option='USER_ENTERED')
    col_a = retry(ws.col_values, 1)
    for d in nb:
        assert a(d["yahoo_sales_forecast_row"]) == "Yahoo販売予想", d["code"]
        assert a(d["yahoo_sales_row"]) == "Yahoo販売実績", d["code"]
    print("行番号検証OK")

    # ===== イベント行 + 係数行 =====
    ev_writes, fmt_reqs, coef_writes = [], [], []
    report = []
    for d in nb:
        r = d["_yev"]
        arr = [""] * (max_c - min_c + 1)
        for d0, d1, lab, fut in events:
            dd = d0
            while dd <= d1:
                c = d_to_c.get(dd)
                if c:
                    arr[c - min_c] = lab
                dd += datetime.timedelta(days=1)
        for dd in five_days:
            c = d_to_c.get(dd)
            if c:
                arr[c - min_c] = "5のつく日"
        for dd in zoro_days:
            c = d_to_c.get(dd)
            if c:
                arr[c - min_c] = "ゾロ目の日"
        ev_writes.append({"range": f"{col_letter(min_c)}{r}:{END}{r}",
                          "values": [arr]})
        fmt_reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": r-1, "endRowIndex": r,
                      "startColumnIndex": min_c-1, "endColumnIndex": max_c},
            "cell": {"userEnteredFormat": {
                "backgroundColor": WHITE,
                "textFormat": {"foregroundColor": BLACK}}},
            "fields": ("userEnteredFormat.backgroundColor,"
                       "userEnteredFormat.textFormat.foregroundColor")}})
        for dd, colr in ([(x, YELLOW) for x in five_days]
                         + [(x, LAVEND) for x in zoro_days]):
            c = d_to_c.get(dd)
            if not c:
                continue
            fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1,
                          "endRowIndex": r,
                          "startColumnIndex": c-1, "endColumnIndex": c},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": colr,
                    "textFormat": {"foregroundColor": colr}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})
        for d0, d1, lab, fut in events:
            c0, c1 = d_to_c.get(d0), d_to_c.get(d1)
            if not (c0 and c1):
                continue
            fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1,
                          "endRowIndex": r,
                          "startColumnIndex": c0-1, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": TEAL,
                    "textFormat": {"foregroundColor": TEAL}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})
            fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1,
                          "endRowIndex": r,
                          "startColumnIndex": c0-1, "endColumnIndex": c0},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": TEAL,
                    "textFormat": {"foregroundColor": BLACK}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})

        m = sales_by_block[d["code"]]
        sdays = sorted(m)
        pool = [dd for dd in sdays if dd not in event_days
                and not is5day(dd) and not iszoro(dd)]

        def baseline(before):
            picked = [m[dd] for dd in reversed(pool) if dd < before][:10]
            if not picked:
                return None
            ws_ = [0.35 * (0.65 ** i) for i in range(len(picked))]
            v = sum(x*w for x, w in zip(picked, ws_)) / sum(ws_)
            return v if v > 0 else None

        base_now = baseline(today)

        def recur_coef(day_fn):
            past = [dd for dd in sdays if day_fn(dd)
                    and dd not in event_days][-10:]
            if past and base_now:
                return round(statistics.mean(m[dd] for dd in past) / base_now, 2)
            return 1

        coef5 = recur_coef(is5day)
        coefz = recur_coef(lambda dd: iszoro(dd) and not is5day(dd))

        hist = []
        per_coef = {}
        for d0, d1, lab, fut in sorted(events):
            vals = [m[dd] for dd in m if d0 <= dd <= d1]
            base = baseline(d0)
            if not vals or base is None:
                continue
            coef = round(statistics.mean(vals) / base, 2)
            per_coef[(d0, d1)] = coef
            hist.append((d1, coef))
        hist.sort()

        arr_c = []
        for c in range(min_c, max_c + 1):
            if c not in date_cols:
                arr_c.append("")
                continue
            dd = col_to_date[c]
            val = 1
            hit = False
            for d0, d1, lab, fut in events:
                if d0 <= dd <= d1:
                    val = per_coef.get((d0, d1), 1)
                    hit = True
                    break
            if not hit:
                if is5day(dd):
                    val = coef5
                elif iszoro(dd):
                    val = coefz
            arr_c.append(val)
        coef_writes.append({
            "range": f"{col_letter(min_c)}{d['_ycoef']}:{END}{d['_ycoef']}",
            "values": [arr_c]})
        report.append(f"  {d['code']}: 5のつく日={coef5}, ゾロ目={coefz}, "
                      f"検出イベント{len(hist)}件")

    for i in range(0, len(ev_writes), 8):
        retry(ws.batch_update, ev_writes[i:i+8], value_input_option='USER_ENTERED')
    for i in range(0, len(fmt_reqs), 80):
        retry(sp.batch_update, {"requests": fmt_reqs[i:i+80]})
    for i in range(0, len(coef_writes), 8):
        retry(ws.batch_update, coef_writes[i:i+8],
              value_input_option='USER_ENTERED')
    print("イベント行・係数行 完了")
    for line in report:
        print(line)

    # ===== ベースライン + ハイブリッド数式 =====
    for d in nb:
        E, WD, WV, CF, HY = (d["_yev"], d["_ywd"], d["_ywv"],
                             d["_ycoef"], d["_yhyb"])
        SL = d["yahoo_sales_row"]

        def conds(ref, extra=""):
            return (f'$C$1:$1<{ref}, $C${E}:${E}="", '
                    f'{extra}$C${SL}:${SL}<>""')

        def wd7(ref):
            cc = conds(ref, 'ARRAYFORMULA(WEEKDAY($C$1:$1,2))<6, ')
            return (f'ROUND(AVERAGE(ARRAY_CONSTRAIN(SORT('
                    f'TRANSPOSE(FILTER($C${SL}:${SL}, {cc})), '
                    f'TRANSPOSE(FILTER($C$1:$1, {cc})), FALSE), 7, 1)), 1)')

        def wavg(ref):
            cc = conds(ref)
            vals = (f'ARRAY_CONSTRAIN(SORT('
                    f'TRANSPOSE(FILTER($C${SL}:${SL}, {cc})), '
                    f'TRANSPOSE(FILTER($C$1:$1, {cc})), FALSE), 10, 1)')
            return f'ROUND(SUMPRODUCT({vals}, {WEIGHTS})/0.9865372085, 2)'

        retry(ws.batch_update, [
            {"range": f"B{WD}", "values": [[f'=IFERROR({wd7("TODAY()")}, "")']]},
            {"range": f"B{WV}", "values": [[f'=IFERROR({wavg("TODAY()")}, "")']]},
        ], value_input_option='USER_ENTERED')

        r_wd, r_wv, r_hy = [], [], []
        for c in range(min_c, max_c + 1):
            if c not in date_cols:
                for lst in (r_wd, r_wv, r_hy):
                    lst.append("")
                continue
            L = col_letter(c)
            r_wd.append(f'=IF({L}$1>TODAY(), $B${WD}, '
                        f'IFERROR({wd7(f"{L}$1")}, $B${WD}))')
            r_wv.append(f'=IF({L}$1>TODAY(), $B${WV}, '
                        f'IFERROR({wavg(f"{L}$1")}, $B${WV}))')
            r_hy.append(f'=IFERROR(ROUND(IF({L}${E}<>"", {L}{WD}, {L}{WV})'
                        f'*{L}{CF}, 1), "")')
        for rng, vals in ((WD, r_wd), (WV, r_wv), (HY, r_hy)):
            retry(ws.update, range_name=f"{col_letter(min_c)}{rng}:{END}{rng}",
                  values=[vals], value_input_option='USER_ENTERED')
        print(f"  {d['code']} 数式完了")

    # ===== config 更新 =====
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "oshima_tab_blocks_config.py")
    src_txt = open(cfg_path, encoding="utf-8").read()
    all_blocks = oshima_tab_blocks_config.get_blocks(args.tab)
    nb_map = {d["code"]: d for d in nb}
    lines = [f'    # {today}: Yahoo長沼5行/ブロック挿入済み']
    lines.append(f'    "{args.tab}": [')
    for b in all_blocks:
        d = nb_map.get(b["code"], b)
        parts = [f'"code": "{d["code"]}"', f'"asin": "{d["asin"]}"']
        for k in d:
            if k in ("code", "asin") or k.startswith("_"):
                continue
            parts.append(f'"{k}": {d[k]}')
        lines.append("        {" + ", ".join(parts) + "},")
    lines.append("    ],")
    pat = re.compile(
        r'(?:^[ \t]*#[^\n]*\n)*^[ \t]*"' + re.escape(args.tab) + r'": \[.*?^\s*\],',
        re.S | re.M)
    assert pat.search(src_txt)
    open(cfg_path, "w", encoding="utf-8").write(
        pat.sub(lambda mm: "\n".join(lines), src_txt, count=1))
    print("config 更新完了")
    print("✅ Yahoo長沼展開 完了")


if __name__ == "__main__":
    main()
