#!/usr/bin/env python3
"""
楽天長沼構成を商品タブへ展開する (マウスピースと同構成)。

各ブロックの「楽天販売予想」の上に5行を挿入:
  楽天イベント長沼        (実績スパイク検出 + スーパーセール公式/予想 + 5と0のつく日)
  直近7平日セール以外平均   (楽天実績の平日7平均)
  直近セール以外加重平均    (楽天実績の加重平均 α=0.35)
  楽天イベント係数長沼      (過去=実測 / 未来=同種の直近5回平均 / 5-0日=実測)
  楽天販売予測長沼        (ハイブリッド: イベント日=平日7×係数 / 通常日=加重×係数)

既存の「楽天販売予想」「楽天販売実績」は変更しない (比較用に保持)。
楽天販売量が少ないタブ (日次中央値<2) はスパイク検出をスキップし、
スーパーセール公式日程 + 5-0日のみ登録する。

使い方:
  python3 rakuten_naganuma_rollout.py --tab "TG-01(在庫)" [--dry-run]
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
RED = {"red": 0.96, "green": 0.75, "blue": 0.75}
TEAL = {"red": 0.72, "green": 0.88, "blue": 0.8}
YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.7}
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
                print(f"    [retry] {delay}s...", file=sys.stderr)
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


def is50(d: datetime.date) -> bool:
    return d.day % 5 == 0


def super_windows(years=(2025, 2026)):
    for y in years:
        for m in (3, 6, 9, 12):
            yield datetime.date(y, m, 4), datetime.date(y, m, 11)


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

    blocks = [b for b in blocks if "rakuten_sales_forecast_row" in b
              and "rakuten_sales_row" in b]
    for b in blocks:
        fc, sl = b["rakuten_sales_forecast_row"], b["rakuten_sales_row"]
        assert a(fc) == "楽天販売予想", f"{b['code']}: R{fc}={a(fc)!r}"
        assert a(sl) == "楽天販売実績", f"{b['code']}: R{sl}={a(sl)!r}"
        assert sl == fc + 1, f"{b['code']}: 予想と実績が隣接していない"
    already = a(blocks[0]["rakuten_sales_forecast_row"] - 1) == "楽天販売予測長沼"
    print(f"[{args.tab}] {len(blocks)}ブロック / 挿入済み: {already}")

    # ===== 販売実績読み込み =====
    sales_by_block = {}
    daily_total = {}
    got = retry(ws.batch_get,
                [f"C{b['rakuten_sales_row']}:{END}{b['rakuten_sales_row']}"
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

    # ===== イベント検出 =====
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

        def label_for(d0, d1):
            for w0, w1 in super_windows():
                if d0 <= w1 and d1 >= w0:
                    return "楽天スーパーセール"
            return "お買い物マラソン"

        events = [(d0, d1, label_for(d0, d1), False) for d0, d1 in periods]
        print(f"スパイク検出: {len(periods)}期間")
    else:
        # 低販売量: 公式スーパーセール窓 (データ範囲内の過去分)
        first = days[0] if days else today
        for w0, w1 in super_windows():
            if w1 < first or w0 > today:
                continue
            events.append((w0, w1, "楽天スーパーセール", False))
        print(f"低販売量タブ → 公式スーパーセール窓 {len(events)}件を使用")

    events.append((datetime.date(2026, 9, 4), datetime.date(2026, 9, 11),
                   "楽天スーパーセール（予想）", True))
    events.append((datetime.date(2026, 12, 4), datetime.date(2026, 12, 11),
                   "楽天スーパーセール（予想）", True))

    event_days = set()
    for d0, d1, lab, fut in events:
        dd = d0
        while dd <= d1:
            event_days.add(dd)
            dd += datetime.timedelta(days=1)
    all_dates = sorted(col_to_date.values())
    fifty_days = [d for d in all_dates if is50(d) and d not in event_days]

    if args.dry_run:
        for d0, d1, lab, fut in events:
            print(f"  {d0}〜{d1} {lab}")
        return

    # ===== 5行挿入 (降順) =====
    if not already:
        ins = sorted((b["rakuten_sales_forecast_row"] for b in blocks),
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
        fc = b["rakuten_sales_forecast_row"]
        d = dict(b)
        for key in list(d.keys()):
            if key.endswith("_row"):
                d[key] = d[key] + S + (5 if d[key] >= fc else 0)
        d["_rev"] = fc + S
        d["_rwd"] = fc + S + 1
        d["_rwv"] = fc + S + 2
        d["_rcoef"] = fc + S + 3
        d["_rhyb"] = fc + S + 4
        # rakuten_sales_forecast_row は元の楽天販売予想 (fc+S+5) を指す
        nb.append(d)

    labels = []
    for d in nb:
        labels += [
            {"range": f"A{d['_rev']}", "values": [["楽天イベント長沼"]]},
            {"range": f"A{d['_rwd']}", "values": [["直近7平日セール以外平均"]]},
            {"range": f"A{d['_rwv']}", "values": [["直近セール以外加重平均"]]},
            {"range": f"A{d['_rcoef']}", "values": [["楽天イベント係数長沼"]]},
            {"range": f"A{d['_rhyb']}", "values": [["楽天販売予測長沼"]]},
        ]
    retry(ws.batch_update, labels, value_input_option='USER_ENTERED')

    col_a = retry(ws.col_values, 1)
    for d in nb:
        assert a(d["rakuten_sales_forecast_row"]) == "楽天販売予想", d["code"]
        assert a(d["rakuten_sales_row"]) == "楽天販売実績", d["code"]
    print("行番号検証OK")

    # ===== イベント行 値+色 / 係数行 =====
    ev_writes, fmt_reqs, coef_writes = [], [], []
    for d in nb:
        r = d["_rev"]
        arr = [""] * (max_c - min_c + 1)
        for d0, d1, lab, fut in events:
            dd = d0
            while dd <= d1:
                c = d_to_c.get(dd)
                if c:
                    arr[c - min_c] = lab
                dd += datetime.timedelta(days=1)
        for dd in fifty_days:
            c = d_to_c.get(dd)
            if c:
                arr[c - min_c] = "5と0のつく日"
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
        for dd in fifty_days:
            c = d_to_c.get(dd)
            if not c:
                continue
            fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1,
                          "endRowIndex": r,
                          "startColumnIndex": c-1, "endColumnIndex": c},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": YELLOW,
                    "textFormat": {"foregroundColor": YELLOW}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})
        for d0, d1, lab, fut in events:
            c0, c1 = d_to_c.get(d0), d_to_c.get(d1)
            if not (c0 and c1):
                continue
            colr = RED if "スーパー" in lab else TEAL
            fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1,
                          "endRowIndex": r,
                          "startColumnIndex": c0-1, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": colr,
                    "textFormat": {"foregroundColor": colr}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})
            fmt_reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r-1,
                          "endRowIndex": r,
                          "startColumnIndex": c0-1, "endColumnIndex": c0},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": colr,
                    "textFormat": {"foregroundColor": BLACK}}},
                "fields": ("userEnteredFormat.backgroundColor,"
                           "userEnteredFormat.textFormat.foregroundColor")}})

        m = sales_by_block[d["code"]]
        sdays = sorted(m)
        pool = [dd for dd in sdays if dd not in event_days and not is50(dd)]

        def baseline(before):
            picked = [m[dd] for dd in reversed(pool) if dd < before][:10]
            if not picked:
                return None
            ws_ = [0.35 * (0.65 ** i) for i in range(len(picked))]
            v = sum(x*w for x, w in zip(picked, ws_)) / sum(ws_)
            return v if v > 0 else None

        past50 = [dd for dd in sdays if is50(dd) and dd not in event_days][-10:]
        base_now = baseline(today)
        coef50 = 1
        if past50 and base_now:
            coef50 = round(statistics.mean(m[dd] for dd in past50) / base_now, 2)

        hist = []
        per_coef = {}
        for d0, d1, lab, fut in sorted(events):
            if fut:
                continue
            vals = [m[dd] for dd in m if d0 <= dd <= d1]
            base = baseline(d0)
            if not vals or base is None:
                continue
            coef = round(statistics.mean(vals) / base, 2)
            per_coef[(d0, d1)] = coef
            hist.append((d1, "スーパー" if "スーパー" in lab else "マラソン", coef))
        hist.sort()

        def predict(cat):
            hs = [c for _, ct, c in hist if ct == cat][-5:]
            if hs:
                return round(statistics.mean(hs), 2)
            hs = [c for _, _, c in hist][-5:]
            return round(statistics.mean(hs), 2) if hs else 1

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
                    val = predict("スーパー" if "スーパー" in lab else "マラソン") \
                        if fut else per_coef.get((d0, d1), 1)
                    hit = True
                    break
            if not hit and is50(dd):
                val = coef50
            arr_c.append(val)
        coef_writes.append({
            "range": f"{col_letter(min_c)}{d['_rcoef']}:{END}{d['_rcoef']}",
            "values": [arr_c]})

    for i in range(0, len(ev_writes), 8):
        retry(ws.batch_update, ev_writes[i:i+8], value_input_option='USER_ENTERED')
    for i in range(0, len(fmt_reqs), 80):
        retry(sp.batch_update, {"requests": fmt_reqs[i:i+80]})
    for i in range(0, len(coef_writes), 8):
        retry(ws.batch_update, coef_writes[i:i+8], value_input_option='USER_ENTERED')
    print("イベント行・係数行 完了")

    # ===== ベースライン + ハイブリッド数式 =====
    for d in nb:
        E, WD, WV, CF, HY = (d["_rev"], d["_rwd"], d["_rwv"],
                             d["_rcoef"], d["_rhyb"])
        SL = d["rakuten_sales_row"]

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
        print(f"  {d['code']} 完了")

    # ===== config 更新 =====
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "oshima_tab_blocks_config.py")
    src_txt = open(cfg_path, encoding="utf-8").read()
    lines = [f'    # {today}: 楽天長沼5行/ブロック挿入済み (rakuten_naganuma_rollout.py)']
    lines.append(f'    "{args.tab}": [')
    all_blocks = oshima_tab_blocks_config.get_blocks(args.tab)
    nb_map = {d["code"]: d for d in nb}
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
    print("✅ 楽天長沼展開 完了")


if __name__ == "__main__":
    main()
