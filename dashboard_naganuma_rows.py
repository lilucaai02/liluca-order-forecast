#!/usr/bin/env python3
"""
ダッシュボード２の各商品行の下に「<商品コード> 長沼」行を作成する。

各長沼行の内容 (ハイブリッド予想ベース):
  B: FBA在庫切れ予測日 (在庫予想長沼行が0以下になる最初の未来日)
  C: 残り日数
  I: FBA在庫数 (昨日) / J: RSL在庫数 (昨日) / K: Stock Crew在庫数 (昨日)
  L: 発注個数予測長沼 (発注予測日からの30日間のハイブリッド+楽天+Yahoo予想合計)
  M: 発注中個数 / N: リードタイム / O: 切れ予測日まであと何日

対応ブロックは oshima_tab_blocks_config から特定
(正規化コード一致 → だめならタブ内の並び順で対応付け)。

使い方:
  python3 dashboard_naganuma_rows.py --dry-run
  python3 dashboard_naganuma_rows.py
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

# ダッシュボードの行グループ → 商品タブ
GROUP_TABS = [
    ("MP-", "マウスピース(在庫)"),
    ("TG-01", "TG-01(在庫)"),
    ("TG-02", "TG-02(在庫)"),
    ("PG-01", "PG-01"),
    ("WB-01", "WB-01(在庫)"),
    ("WB-02", "WB-02"),
    ("GC-01", "GC-01(在庫)"),
    ("gc-02", "GC-02(在庫)"),
    ("GC-02", "GC-02(在庫)"),
    ("TS-0", "TS-01"),
    ("PCI-01", "PCI-01"),
    ("DS-01", "DS-01 (在庫) "),
]


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


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


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
    ws = retry(sp.worksheet, "ダッシュボード２")

    # ===== 1) ダッシュボードの商品行を把握 =====
    col_a = retry(ws.col_values, 1)
    naganuma_done = set()
    product_rows = []  # (row, code)
    for i, v in enumerate(col_a, 1):
        v = (v or "").strip()
        if i < 3 or not v:
            continue
        if v.endswith("長沼"):
            naganuma_done.add(norm(v.replace("長沼", "")))
            continue
        product_rows.append((i, v))

    # ===== 2) 商品タブ側の行番号を解決 =====
    # タブごとの利用ブロックカーソル (順序フォールバック用)
    cursor = {}
    tab_labels = {}  # タブ名 → col_a (ラベル検証用)

    def tab_for(code: str):
        for prefix, tab in GROUP_TABS:
            if code.startswith(prefix):
                return tab
        return None

    def block_for(code: str):
        tab = tab_for(code)
        if not tab:
            return None, None
        blocks = oshima_tab_blocks_config.get_blocks(tab)
        n = norm(code)
        for b in blocks:
            if norm(b["code"]) == n:
                return tab, b
        # 部分一致
        for b in blocks:
            if n in norm(b["code"]) or norm(b["code"]) in n:
                return tab, b
        # 順序フォールバック
        idx = cursor.get(tab, 0)
        if idx < len(blocks):
            cursor[tab] = idx + 1
            return tab, blocks[idx]
        return tab, None

    def tab_col_a(tab):
        if tab not in tab_labels:
            wst = retry(sp.worksheet, tab)
            tab_labels[tab] = retry(wst.col_values, 1)
        return tab_labels[tab]

    plans = []
    skipped = []
    for row, code in product_rows:
        if norm(code) in naganuma_done:
            skipped.append((code, "長沼行あり"))
            continue
        tab, blk = block_for(code)
        if not blk:
            skipped.append((code, "ブロック未対応"))
            continue
        F = blk["sales_forecast_row"]
        G = blk["forecast_row"]
        hyb = F + 1              # アマゾン長沼予想ハイブリッド
        inv = G + 1              # 在庫予想長沼
        act = G + 2              # FBA在庫実績 (旧G+3から-1)
        # ラベル検証
        la = tab_col_a(tab)

        def lab(r):
            return la[r-1].strip() if r-1 < len(la) else ""

        checks = {
            hyb: "アマゾン長沼予想ハイブリッド",
            inv: "在庫予想長沼",
        }
        ok = all(lab(r) == want for r, want in checks.items())
        if not ok:
            skipped.append((code, f"タブ構造不一致 {tab} R{hyb}/{inv}"))
            continue
        # FBA在庫実績はinvの直下
        assert lab(inv + 1) == "FBA在庫実績", f"{code}: R{inv+1}={lab(inv+1)!r}"
        # 発注中 / リードタイム / 総数発注予測日 をラベル検索 (TO行以降)
        to = blk["to_row"]
        aux = {}
        for r in range(to, min(to + 25, len(la) + 1)):
            t = lab(r)
            if t == "発注中" or t == "リードタイム" or t.startswith("総数発注予測日"):
                key = "発注中" if t == "発注中" else (
                    "LT" if t == "リードタイム" else "発注日")
                if key not in aux:
                    aux[key] = r
        # 発注中はTOより上にある場合もある
        if "発注中" not in aux:
            for r in range(max(1, to - 15), to):
                if lab(r) == "発注中":
                    aux["発注中"] = r
                    break
        plans.append({
            "row": row, "code": code, "tab": tab,
            "hyb": hyb, "inv": inv, "act": inv + 1,
            "rsl": blk.get("rsl_stock_row"),
            "sc": blk.get("stock_crew_stock_row"),
            "rak_fc": blk.get("rakuten_sales_forecast_row"),
            "yah_fc": blk.get("yahoo_sales_forecast_row"),
            "hatchu": aux.get("発注中"),
            "lt": aux.get("LT"),
            "hatchubi": aux.get("発注日"),
        })

    print(f"作成対象: {len(plans)}行 / スキップ: {len(skipped)}")
    for code, why in skipped:
        print(f"  skip {code}: {why}")
    if args.dry_run:
        for p in plans[:10]:
            print(f"  {p['code']}: tab={p['tab']} hyb=R{p['hyb']} inv=R{p['inv']} "
                  f"発注中=R{p['hatchu']} LT=R{p['lt']} 発注日=R{p['hatchubi']}")
        return

    # ===== 3) 行挿入 (下から上へ) =====
    reqs = []
    for p in sorted(plans, key=lambda x: -x["row"]):
        reqs.append({"insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": p["row"], "endIndex": p["row"] + 1},
            "inheritFromBefore": True}})
    for i in range(0, len(reqs), 60):
        retry(sp.batch_update, {"requests": reqs[i:i+60]})
    print(f"{len(plans)}行 挿入完了")

    # 挿入後の行番号: 自分より上にある挿入数だけ下がる
    plans.sort(key=lambda x: x["row"])
    for idx, p in enumerate(plans):
        p["new_row"] = p["row"] + 1 + idx  # idx = 自分より上の挿入数

    # ===== 4) 数式書き込み =====
    data = []
    for p in plans:
        r = p["new_row"]
        T = f"'{p['tab']}'"
        inv = p["inv"]
        mini = (f'MINIFS({T}!$1:$1,{T}!${inv}:${inv},"<=0",'
                f'{T}!$1:$1,">"&TODAY())')
        data.append({"range": f"A{r}", "values": [[f"{p['code']} 長沼"]]})
        data.append({"range": f"B{r}",
                     "values": [[f'=IF({mini}=0,"",{mini})']]})
        data.append({"range": f"C{r}",
                     "values": [[f'=IF($B{r}="","",$B{r}-TODAY())']]})

        def idx_f(row_no):
            if not row_no:
                return ""
            return (f'=IFERROR(INDEX({T}!${row_no}:${row_no},'
                    f'MATCH(TODAY()-1,{T}!$1:$1,0)),"")')

        data.append({"range": f"I{r}", "values": [[idx_f(p["act"])]]})
        data.append({"range": f"J{r}", "values": [[idx_f(p["rsl"])]]})
        data.append({"range": f"K{r}", "values": [[idx_f(p["sc"])]]})
        # L: 発注個数予測長沼
        if p["hatchubi"] and p["lt"]:
            hb, lt, hyb = p["hatchubi"], p["lt"], p["hyb"]
            parts = [f'N({T}!IT{hyb}:ACQ{hyb})']
            if p["rak_fc"]:
                parts.append(f'N({T}!IT{p["rak_fc"]}:ACQ{p["rak_fc"]})')
            if p["yah_fc"]:
                parts.append(f'N({T}!IT{p["yah_fc"]}:ACQ{p["yah_fc"]})')
            total = f'ARRAYFORMULA({"+".join(parts)})'
            W = (f'IF({T}!$B${hb}<>"", {T}!$B${hb}, '
                 f'IF({mini}=0, "", {mini}-30-{T}!$B${lt}))')
            cond = (f'({T}!$IT$1:$ACQ$1>{W})*({T}!$IT$1:$ACQ$1<={W}+30)')
            data.append({"range": f"L{r}", "values": [[
                f'=IFERROR(IF({W}="", "", '
                f'ROUNDUP(SUM(FILTER({total}, {cond})))),"")']]})
        # M: 発注中 / N: リードタイム
        if p["hatchu"]:
            data.append({"range": f"M{r}", "values": [[
                f'=IFERROR(INDEX({T}!${p["hatchu"]}:${p["hatchu"]},'
                f'MATCH($A$2,{T}!$1:$1,0)),"")']]})
        if p["lt"]:
            data.append({"range": f"N{r}",
                         "values": [[f'={T}!$B${p["lt"]}']]})
        data.append({"range": f"O{r}", "values": [[
            f'=IF(B{r}="","",IFERROR(DATEDIF($A$2,B{r},"D"),"予測時間過ぎました"))']]})

    for i in range(0, len(data), 60):
        retry(ws.batch_update, data[i:i+60], value_input_option='USER_ENTERED')
    print(f"数式書き込み完了 ({len(data)}セル)")
    print("✅ ダッシュボード長沼行 作成完了")


if __name__ == "__main__":
    main()
