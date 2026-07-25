#!/usr/bin/env python3
"""
長沼アップグレード済みタブをハイブリッド構成に移行する。

各ブロックで:
  - 平均行2本を「直近7平日セール以外平均」「直近セール以外加重平均」に統一
  - 長沼予想７日の行を「アマゾン長沼予想ハイブリッド」に書き換え
    (セール日=平日7平均×係数長沼 / 通常日=加重平均×係数長沼)
  - 長沼予想３０日・在庫予想長沼３０日 の行を削除
  - 在庫予想長沼７日 → 「在庫予想長沼」(ハイブリッド予想ベースのチェーン) に書き換え
  - oshima_tab_blocks_config.py を更新

使い方:
  python3 oshima_hybrid_migration.py --tab "マウスピース(在庫)"
  python3 oshima_hybrid_migration.py --tab "TG-01(在庫)"
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
WEIGHTS = ("{0.35;0.2275;0.147875;0.09611875;0.0624771875;"
           "0.040610171875;0.02639661171875;0.0171577976171875;"
           "0.0111525684511719;0.0072491694932617}")


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
    print(f"[{args.tab}] {len(blocks)}ブロック")

    col_a = retry(ws.col_values, 1)

    def a(r):
        return col_a[r - 1].strip() if r - 1 < len(col_a) else ""

    # 検証: 現構造 (長沼アップグレード済み前提)
    for blk in blocks:
        F = blk["sales_forecast_row"]
        G = blk["forecast_row"]
        assert a(F) == "amazon販売予想", f"{blk['code']}: R{F}={a(F)!r}"
        assert a(F + 1) == "アマゾン長沼予想７日", f"{blk['code']}: R{F+1}={a(F+1)!r}"
        assert a(F + 2) == "アマゾン長沼予想３０日", f"{blk['code']}: R{F+2}"
        assert a(G + 1) == "在庫予想長沼７日", f"{blk['code']}: R{G+1}"
        assert a(G + 2) == "在庫予想長沼３０日", f"{blk['code']}: R{G+2}"
    print("構造検証OK")

    # ===== 1) 削除 (全ブロックの F+2, G+2 を降順で) =====
    del_rows = []
    for blk in blocks:
        del_rows += [blk["sales_forecast_row"] + 2, blk["forecast_row"] + 2]
    del_rows.sort(reverse=True)
    reqs = [{"deleteDimension": {"range": {
        "sheetId": ws.id, "dimension": "ROWS",
        "startIndex": r - 1, "endIndex": r}}} for r in del_rows]
    for i in range(0, len(reqs), 60):
        retry(sp.batch_update, {"requests": reqs[i:i+60]})
    print(f"削除完了: {len(del_rows)}行")

    # ===== 2) 新しい行番号 =====
    def shift(row, F, G, k):
        s = -2 * k
        if row > F + 2:
            s -= 1
        if row > G + 2:
            s -= 1
        return row + s

    nb = []
    for k, blk in enumerate(blocks):
        F, G = blk["sales_forecast_row"], blk["forecast_row"]
        d = dict(blk)
        for key in list(d.keys()):
            if key.endswith("_row"):
                d[key] = shift(d[key], F, G, k)
        E = F - 7
        d["_E"] = shift(E, F, G, k)
        d["_nag"] = shift(E + 1, F, G, k)
        d["_avg7"] = shift(E + 2, F, G, k)
        d["_wavg"] = shift(E + 3, F, G, k)
        d["_coef"] = shift(E + 5, F, G, k)
        d["_hyb"] = shift(F + 1, F, G, k)
        d["_inv"] = shift(G + 1, F, G, k)
        d["_act"] = shift(G + 3, F, G, k)
        nb.append(d)

    # ラベル
    label_data = []
    for d in nb:
        label_data += [
            {"range": f"A{d['_avg7']}", "values": [["直近7平日セール以外平均"]]},
            {"range": f"A{d['_wavg']}", "values": [["直近セール以外加重平均"]]},
            {"range": f"A{d['_hyb']}", "values": [["アマゾン長沼予想ハイブリッド"]]},
            {"range": f"A{d['_inv']}", "values": [["在庫予想長沼"]]},
        ]
    retry(ws.batch_update, label_data, value_input_option='USER_ENTERED')

    # 検証
    col_a = retry(ws.col_values, 1)
    for d in nb:
        assert a(d["_hyb"]) == "アマゾン長沼予想ハイブリッド", \
            f"{d['code']}: R{d['_hyb']}={a(d['_hyb'])!r}"
        assert a(d["sales_row"]) == "amazonFBA販売実績", \
            f"{d['code']}: R{d['sales_row']}={a(d['sales_row'])!r}"
        assert a(d["_act"]) == "FBA在庫実績", f"{d['code']}: R{d['_act']}"
    print("行番号検証OK")

    # ===== 3) 数式再生成 =====
    row1_raw = retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    date_cols = {i for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    min_c, max_c = min(date_cols), max(date_cols)
    END = col_letter(max_c)

    for d in nb:
        S, E, N = d["sales_row"], d["_E"], d["_nag"]
        A7, WV, CF, HY = d["_avg7"], d["_wavg"], d["_coef"], d["_hyb"]
        INV, ACT = d["_inv"], d["_act"]
        CHG, FR, TO = d["change_qty_row"], d["from_row"], d["to_row"]

        def conds(ref, extra=""):
            return (f'$C$1:$1<{ref}, '
                    f'$C${E}:${E}="", $C${N}:${N}="", '
                    f'{extra}$C${S}:${S}<>""')

        def wd7(ref):
            cc = conds(ref, f'ARRAYFORMULA(WEEKDAY($C$1:$1,2))<6, ')
            return (f'ROUND(AVERAGE(ARRAY_CONSTRAIN(SORT('
                    f'TRANSPOSE(FILTER($C${S}:${S}, {cc})), '
                    f'TRANSPOSE(FILTER($C$1:$1, {cc})), FALSE), 7, 1)), 1)')

        def wavg(ref):
            cc = conds(ref)
            vals = (f'ARRAY_CONSTRAIN(SORT('
                    f'TRANSPOSE(FILTER($C${S}:${S}, {cc})), '
                    f'TRANSPOSE(FILTER($C$1:$1, {cc})), FALSE), 10, 1)')
            return f'ROUND(SUMPRODUCT({vals}, {WEIGHTS})/0.9865372085, 1)'

        LASTACT = (f'LOOKUP(2, ARRAYFORMULA(1/(($C${ACT}:${ACT}<>"")'
                   f'*($C${ACT}:${ACT}<>0)*($C$1:$1<=TODAY()))), $C${ACT}:${ACT})')

        retry(ws.batch_update, [
            {"range": f"B{A7}", "values": [[f'=IFERROR({wd7("TODAY()")}, "")']]},
            {"range": f"B{WV}", "values": [[f'=IFERROR({wavg("TODAY()")}, "")']]},
        ], value_input_option='USER_ENTERED')

        r_a7, r_wv, r_hy, r_inv = [], [], [], []
        for c in range(min_c, max_c + 1):
            if c not in date_cols:
                for lst in (r_a7, r_wv, r_hy, r_inv):
                    lst.append("")
                continue
            L = col_letter(c)
            P = col_letter(c - 1)
            r_a7.append(f'=IF({L}$1>TODAY(), $B${A7}, '
                        f'IFERROR({wd7(f"{L}$1")}, $B${A7}))')
            r_wv.append(f'=IF({L}$1>TODAY(), $B${WV}, '
                        f'IFERROR({wavg(f"{L}$1")}, $B${WV}))')
            r_hy.append(f'=IFERROR(ROUND(IF({L}${N}<>"", {L}{A7}, {L}{WV})'
                        f'*{L}{CF}, 1), "")')
            seed = f'IF({P}$1<=TODAY(), {LASTACT}, {P}{INV})'
            chain = (f'{seed}+IF({P}{TO}="FBA",{P}{CHG},0)-{P}{HY}'
                     f'-IF({P}{FR}="FBA",{P}{CHG},0)')
            past = f'IF(OR({L}{ACT}="",{L}{ACT}=0), "", {L}{ACT})'
            r_inv.append(f'=IF({L}$1<=TODAY(), {past}, ROUND({chain}, 1))')

        payload = [
            {"range": f"{col_letter(min_c)}{A7}:{END}{A7}", "values": [r_a7]},
            {"range": f"{col_letter(min_c)}{WV}:{END}{WV}", "values": [r_wv]},
            {"range": f"{col_letter(min_c)}{HY}:{END}{HY}", "values": [r_hy]},
            {"range": f"{col_letter(min_c)}{INV}:{END}{INV}", "values": [r_inv]},
        ]
        for p in payload:
            retry(ws.batch_update, [p], value_input_option='USER_ENTERED')
        print(f"  {d['code']} 完了")

    # ===== 4) config 更新 =====
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "oshima_tab_blocks_config.py")
    src_txt = open(cfg_path, encoding="utf-8").read()
    lines = [f'    # {datetime.date.today()}: ハイブリッド移行済み (oshima_hybrid_migration.py)']
    lines.append(f'    "{args.tab}": [')
    for d in nb:
        keys = [k for k in d if not k.startswith("_")]
        parts = [f'"code": "{d["code"]}"', f'"asin": "{d["asin"]}"']
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
    assert pat.search(src_txt), "config にタブ定義なし"
    open(cfg_path, "w", encoding="utf-8").write(
        pat.sub(lambda m: new_section, src_txt, count=1))
    print("config 更新完了")
    print("\n✅ ハイブリッド移行完了")


if __name__ == "__main__":
    main()
