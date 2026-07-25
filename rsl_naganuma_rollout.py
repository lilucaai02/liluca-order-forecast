#!/usr/bin/env python3
"""
RSL在庫予想長沼 行を全タブの各ブロックに追加する。

RSL在庫予想の直下に1行挿入し、Amazonの在庫予想長沼と同じチェーン式を設定:
  過去日 = RSL在庫実績 (空/0は空欄)
  未来日 = 直近実績を起点に、楽天販売予測長沼を差し引き
           (在庫変更のFROM/TO="RSL"も反映)

使い方:
  python3 rsl_naganuma_rollout.py --tab "マウスピース(在庫)"
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
    date_cols = {i for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    min_c, max_c = min(date_cols), max(date_cols)
    END = col_letter(max_c)

    col_a = retry(ws.col_values, 1)

    def a(r):
        return col_a[r - 1].strip() if r - 1 < len(col_a) else ""

    blocks = [b for b in blocks if "rsl_forecast_row" in b]
    for b in blocks:
        rf = b["rsl_forecast_row"]
        assert a(rf) == "RSL在庫予想", f"{b['code']}: R{rf}={a(rf)!r}"
        assert a(b["rsl_stock_row"]) == "RSL在庫実績", b["code"]
        # ハイブリッド行の位置はタブにより異なる (fc自身 or fc-1) → ラベルで特定
        fc = b["rakuten_sales_forecast_row"]
        if a(fc) == "楽天販売予測長沼":
            b["_hyb_abs"] = fc
        elif a(fc - 1) == "楽天販売予測長沼":
            b["_hyb_abs"] = fc - 1
        else:
            raise AssertionError(
                f"{b['code']}: 楽天販売予測長沼が R{fc-1}/R{fc} にない "
                f"({a(fc-1)!r}/{a(fc)!r})")
    already = a(blocks[0]["rsl_forecast_row"] + 1) == "RSL在庫予想長沼"
    print(f"[{args.tab}] {len(blocks)}ブロック / 挿入済み: {already}")

    if not already:
        ins = sorted((b["rsl_forecast_row"] for b in blocks), reverse=True)
        reqs = [{"insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": r, "endIndex": r + 1},
            "inheritFromBefore": True}} for r in ins]
        for i in range(0, len(reqs), 60):
            retry(sp.batch_update, {"requests": reqs[i:i+60]})
        print("1行×ブロック 挿入完了")

    nb = []
    for k, b in enumerate(blocks):
        S = k
        rf = b["rsl_forecast_row"]
        hyb_abs = b["_hyb_abs"]
        d = {kk: v for kk, v in b.items() if not kk.startswith("_")}
        for key in list(d.keys()):
            if key.endswith("_row"):
                d[key] = d[key] + S + (1 if d[key] > rf else 0)
        d["_rsln"] = rf + S + 1
        d["_hyb"] = hyb_abs + S + (1 if hyb_abs > rf else 0)
        nb.append(d)

    labels = [{"range": f"A{d['_rsln']}", "values": [["RSL在庫予想長沼"]]}
              for d in nb]
    retry(ws.batch_update, labels, value_input_option='USER_ENTERED')

    col_a = retry(ws.col_values, 1)
    for d in nb:
        assert a(d["_rsln"]) == "RSL在庫予想長沼", d["code"]
        assert a(d["rsl_stock_row"]) == "RSL在庫実績", \
            f"{d['code']}: R{d['rsl_stock_row']}={a(d['rsl_stock_row'])!r}"
    print("行番号検証OK")

    for d in nb:
        ME, ACT, HYB = d["_rsln"], d["rsl_stock_row"], d["_hyb"]
        CHG, FR, TO = d["change_qty_row"], d["from_row"], d["to_row"]
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
            chain = (f'{seed}+IF({P}{TO}="RSL",{P}{CHG},0)-{P}{HYB}'
                     f'-IF({P}{FR}="RSL",{P}{CHG},0)')
            past = f'IF(OR({L}{ACT}="",{L}{ACT}=0), "", {L}{ACT})'
            vals.append(f'=IF({L}$1<=TODAY(), {past}, ROUND({chain}, 1))')
        retry(ws.update, range_name=f"{col_letter(min_c)}{ME}:{END}{ME}",
              values=[vals], value_input_option='USER_ENTERED')
        print(f"  {d['code']} 完了")

    # config 更新
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "oshima_tab_blocks_config.py")
    src_txt = open(cfg_path, encoding="utf-8").read()
    all_blocks = oshima_tab_blocks_config.get_blocks(args.tab)
    nb_map = {d["code"]: d for d in nb}
    lines = [f'    # {today}: RSL在庫予想長沼 1行/ブロック挿入済み']
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
    print("✅ RSL在庫予想長沼 完了")


if __name__ == "__main__":
    main()
