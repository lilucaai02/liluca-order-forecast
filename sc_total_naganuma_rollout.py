#!/usr/bin/env python3
"""
Stock Crew在庫予想長沼 / 在庫総数予想長沼 行を全タブの各ブロックに追加する。

ダッシュボード２の長沼行 J/K/L (SC切れ・総数切れ・総数発注予測日) の土台。

  Stock Crew在庫予想長沼:
    過去日 = Stock Crew在庫実績 (空/0は空欄)
    未来日 = 直近実績を起点に、Yahoo販売予測長沼を差し引き
             (在庫変更のFROM/TO="Stock Crew"も反映)
  在庫総数予想長沼:
    元の在庫総数予想のSUM構成のうち FBA在庫予想→在庫予想長沼、
    RSL在庫予想→RSL在庫予想長沼、Stock Crew在庫予想→SC長沼 に置換

使い方:
  python3 sc_total_naganuma_rollout.py --tab "マウスピース(在庫)"
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
import oshima_tab_blocks_config

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)
SC_LABEL = "Stock Crew在庫予想長沼"
TOTAL_LABEL = "在庫総数予想長沼"


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", required=True)
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
    fut_c = d_to_c.get(today + datetime.timedelta(days=5))
    assert fut_c, "未来列が見つからない"

    col_a = retry(ws.col_values, 1)

    def a(r):
        return col_a[r - 1].strip() if r - 1 < len(col_a) else ""

    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    info = []
    for bi, b in enumerate(blocks):
        lo, hi = bounds[bi], bounds[bi + 1]
        lab2row = {}
        for r in range(lo, hi):
            v = a(r)
            if v and v not in lab2row:
                lab2row[v] = r
        d = dict(b)
        d["_sc_fc"] = lab2row.get("Stock Crew在庫予想")
        d["_sc_act"] = lab2row.get("Stock Crew在庫実績")
        d["_total_fc"] = lab2row.get("在庫総数予想")
        d["_yahoo_n"] = lab2row.get("Yahoo販売予測長沼")
        d["_fba_n"] = lab2row.get("在庫予想長沼")
        d["_rsl_n"] = lab2row.get("RSL在庫予想長沼")
        d["_sc_done"] = lab2row.get(SC_LABEL)
        d["_total_done"] = lab2row.get(TOTAL_LABEL)
        assert d["_yahoo_n"], f"{b['code']}: Yahoo販売予測長沼がない"
        if d["_sc_fc"]:
            assert d["_sc_act"] == d["_sc_fc"] + 1 + (1 if d["_sc_done"] else 0), \
                f"{b['code']}: SC実績位置 {d['_sc_act']} vs 予想 {d['_sc_fc']}"
        info.append(d)

    already = any(d["_sc_done"] or d["_total_done"] for d in info)
    if already:
        # 中途半端な状態 (一部ブロックのみ挿入済み) は誤上書き防止のため中断
        for d in info:
            if d["_sc_fc"] and not d["_sc_done"]:
                raise AssertionError(f"{d['code']}: SC長沼が部分的に未挿入")
            if d["_total_fc"] and not d["_total_done"]:
                raise AssertionError(f"{d['code']}: 総数長沼が部分的に未挿入")
    n_sc = sum(1 for d in info if d["_sc_fc"])
    n_tt = sum(1 for d in info if d["_total_fc"])
    print(f"[{args.tab}] {len(info)}ブロック / SC対象:{n_sc} 総数対象:{n_tt} "
          f"/ 挿入済み: {already}")

    # ===== 元の在庫総数予想のSUM構成を取得 (挿入前の行番号) =====
    FL = col_letter(fut_c)
    ranges = [f"{FL}{d['_total_fc']}" if d["_total_fc"] else "A1"
              for d in info]
    got = retry(ws.batch_get, ranges, value_render_option='FORMULA')
    for d, g in zip(info, got):
        d["_sum_rows"] = None
        if not d["_total_fc"]:
            continue
        f = g[0][0] if g and g[0] else ""
        if isinstance(f, str) and f.startswith("=") and ":" not in f:
            rows = [int(m) for m in re.findall(r"[A-Z]{1,3}(\d+)", f)
                    if int(m) > 1]
            # 自己参照 (=前日セル参照のチェーン式) はSUM構成でないので不採用
            if rows and d["_total_fc"] not in rows:
                d["_sum_rows"] = sorted(set(rows))
        if d["_sum_rows"] is None:
            # フォールバック: ラベルから構成
            lo, hi = bounds[info.index(d)], bounds[info.index(d) + 1]
            comp = []
            for lbl in ("FBA在庫予想", "RSL在庫予想", "Stock Crew在庫予想",
                        "荒瀬倉庫 amazon用在庫予定", "荒瀬倉庫 amazon以外在庫予定",
                        "事務所 amazon用在庫予定", "事務所 amazon以外在庫予定",
                        "イーウーパスポート　中国在庫予定", "移動中予定", "発注中"):
                for r in range(lo, hi):
                    if a(r) == lbl:
                        comp.append(r)
                        break
            d["_sum_rows"] = comp
            print(f"  {d['code']}: SUM式パース不可 → ラベル構成 {comp}")

    # ===== 挿入 =====
    if not already:
        points = []
        for d in info:
            if d["_sc_fc"]:
                points.append(d["_sc_fc"])
            if d["_total_fc"]:
                points.append(d["_total_fc"])
        points = sorted(set(points))
        reqs = [{"insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": r, "endIndex": r + 1},
            "inheritFromBefore": True}} for r in sorted(points, reverse=True)]
        for i in range(0, len(reqs), 60):
            retry(sp.batch_update, {"requests": reqs[i:i + 60]})
        print(f"{len(points)}行 挿入完了")

        def shift(r):
            return r + sum(1 for p in points if p < r)
    else:
        # 挿入済み: 現在の行番号をそのまま使う
        def shift(r):
            return r

    nb = []
    for d in info:
        e = {k: v for k, v in d.items() if not k.startswith("_")}
        for key in list(e.keys()):
            if key.endswith("_row"):
                e[key] = shift(e[key])
        e["code"] = d["code"]
        e["_yahoo_n"] = shift(d["_yahoo_n"])
        e["_fba_n"] = shift(d["_fba_n"]) if d["_fba_n"] else None
        e["_rsl_n"] = shift(d["_rsl_n"]) if d["_rsl_n"] else None
        if d["_sc_fc"]:
            e["_sc_act"] = shift(d["_sc_act"])
            e["_sc_n"] = (d["_sc_done"] if already and d["_sc_done"]
                          else shift(d["_sc_fc"]) + 1)
        else:
            e["_sc_n"] = None
        if d["_total_fc"]:
            e["_total_n"] = (d["_total_done"] if already and d["_total_done"]
                             else shift(d["_total_fc"]) + 1)
            e["_sum_rows"] = d["_sum_rows"]
        else:
            e["_total_n"] = None
        nb.append(e)

    labels = []
    for e in nb:
        if e["_sc_n"]:
            labels.append({"range": f"A{e['_sc_n']}", "values": [[SC_LABEL]]})
        if e["_total_n"]:
            labels.append({"range": f"A{e['_total_n']}",
                           "values": [[TOTAL_LABEL]]})
    retry(ws.batch_update, [dict(x) for x in labels],
          value_input_option='USER_ENTERED')
    col_a = retry(ws.col_values, 1)
    for e in nb:
        if e["_sc_n"]:
            assert a(e["_sc_n"]) == SC_LABEL, e["code"]
            assert a(e["_sc_act"]) == "Stock Crew在庫実績", \
                f"{e['code']}: R{e['_sc_act']}={a(e['_sc_act'])!r}"
        if e["_total_n"]:
            assert a(e["_total_n"]) == TOTAL_LABEL, e["code"]
        assert a(e["_yahoo_n"]) == "Yahoo販売予測長沼", e["code"]
    print("行番号検証OK")

    # ===== 数式書き込み =====
    for e in nb:
        if e["_sc_n"]:
            ME, ACT = e["_sc_n"], e["_sc_act"]
            HYB = e["_yahoo_n"]
            CHG = e["change_qty_row"]
            FR, TO = e["from_row"], e["to_row"]
            LASTACT = (f'LOOKUP(2, ARRAYFORMULA(1/(($C${ACT}:${ACT}<>"")'
                       f'*($C${ACT}:${ACT}<>0)*($C$1:$1<=TODAY()))), '
                       f'$C${ACT}:${ACT})')
            vals = []
            for c in range(min_c, max_c + 1):
                if c not in date_cols:
                    vals.append("")
                    continue
                L = col_letter(c)
                P = col_letter(c - 1)
                seed = f'IF({P}$1<=TODAY(), {LASTACT}, {P}{ME})'
                chain = (f'{seed}+IF({P}{TO}="Stock Crew",{P}{CHG},0)'
                         f'-{P}{HYB}-IF({P}{FR}="Stock Crew",{P}{CHG},0)')
                past = f'IF(OR({L}{ACT}="",{L}{ACT}=0), "", {L}{ACT})'
                vals.append(f'=IF({L}$1<=TODAY(), {past}, ROUND({chain}, 1))')
            retry(ws.update, range_name=f"{col_letter(min_c)}{ME}:{END}{ME}",
                  values=[vals], value_input_option='USER_ENTERED')
        if e["_total_n"]:
            MT = e["_total_n"]
            comps = []
            for r in e["_sum_rows"]:
                lbl = ""
                # 挿入前の行 r のラベルはシフト後の位置で判定
                rr = shift(r)
                lbl = a(rr)
                if lbl == "FBA在庫予想" and e["_fba_n"]:
                    comps.append(e["_fba_n"])
                elif lbl == "RSL在庫予想" and e["_rsl_n"]:
                    comps.append(e["_rsl_n"])
                elif lbl == "Stock Crew在庫予想" and e["_sc_n"]:
                    comps.append(e["_sc_n"])
                else:
                    comps.append(rr)
            comps = sorted(set(comps))
            vals = []
            for c in range(min_c, max_c + 1):
                if c not in date_cols:
                    vals.append("")
                    continue
                L = col_letter(c)
                refs = ",".join(f"{L}{r}" for r in comps)
                vals.append(f"=SUM({refs})")
            retry(ws.update, range_name=f"{col_letter(min_c)}{MT}:{END}{MT}",
                  values=[vals], value_input_option='USER_ENTERED')
        print(f"  {e['code']} 完了 (SC長沼={e['_sc_n']} 総数長沼={e['_total_n']})")

    # ===== config 更新 =====
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "oshima_tab_blocks_config.py")
    src_txt = open(cfg_path, encoding="utf-8").read()
    lines = [f'    # {today}: SC在庫予想長沼/在庫総数予想長沼 挿入済み']
    lines.append(f'    "{args.tab}": [')
    for e in nb:
        parts = [f'"code": "{e["code"]}"', f'"asin": "{e["asin"]}"']
        for k in e:
            if k in ("code", "asin") or k.startswith("_"):
                continue
            parts.append(f'"{k}": {e[k]}')
        if e["_sc_n"]:
            parts.append(f'"sc_naganuma_row": {e["_sc_n"]}')
        if e["_total_n"]:
            parts.append(f'"total_naganuma_row": {e["_total_n"]}')
        lines.append("        {" + ", ".join(parts) + "},")
    lines.append("    ],")
    pat = re.compile(
        r'(?:^[ \t]*#[^\n]*\n)*^[ \t]*"' + re.escape(args.tab)
        + r'": \[.*?^\s*\],', re.S | re.M)
    assert pat.search(src_txt)
    open(cfg_path, "w", encoding="utf-8").write(
        pat.sub(lambda mm: "\n".join(lines), src_txt, count=1))
    print("config 更新完了")
    print(f"✅ {SC_LABEL}/{TOTAL_LABEL} 完了")


if __name__ == "__main__":
    main()
