#!/usr/bin/env python3
"""DS-01方式の再構成を商品タブへ適用する。

各ブロックで:
 1. セール価格行 → 「販売価格」に改名 (価格データは --prices で別途投入)
 2. 元システム行を削除:
    通常価格販売 / amazonイベント / amazonイベント係数 / amazon販売予想 /
    Amazon平日平均販売点数 / 楽天販売予想 / Yahoo販売予想 /
    FBA在庫予想 / RSL在庫予想 / Stock Crew在庫予想
 3. 削除前に参照を長沼へ付け替え (ブロック内全行をスキャン):
    amazon販売予想→ハイブリッド, 楽天販売予想→楽天長沼, Yahoo販売予想→Yahoo長沼,
    FBA在庫予想→在庫予想長沼, RSL在庫予想→RSL長沼, SC在庫予想→SC長沼,
    ベースラインの amazonイベント条件 ($C$n:$n="") は除去
 4. 全体の販売実績 = 未来空白ガード付きSUMに書き換え
 5. config再生成

使い方:
  python3 apply_ds01_restructure.py --tab "マウスピース(在庫)"   # 構造変更
  python3 apply_ds01_restructure.py --tab "..." --prices        # 販売価格投入
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
import oshima_tab_blocks_config

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)
DELETE_LABELS = ["通常価格販売", "amazonイベント", "amazonイベント係数",
                 "amazon販売予想", "Amazon平日平均販売点数",
                 "楽天販売予想", "Yahoo販売予想",
                 "FBA在庫予想", "RSL在庫予想", "Stock Crew在庫予想"]
REPOINT = {"amazon販売予想": "アマゾン長沼予想ハイブリッド",
           "楽天販売予想": "楽天販売予測長沼",
           "Yahoo販売予想": "Yahoo販売予測長沼",
           "FBA在庫予想": "在庫予想長沼",
           "RSL在庫予想": "RSL在庫予想長沼",
           "Stock Crew在庫予想": "Stock Crew在庫予想長沼"}


def retry(fn, *a, **k):
    from gspread.exceptions import APIError
    delay = 20
    for i in range(10):
        try:
            return fn(*a, **k)
        except APIError as e:
            if any(x in str(e) for x in ("429", "500", "503", "409")) and i < 9:
                time.sleep(delay)
                delay = min(delay * 2, 180)
            else:
                raise


def cl(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def open_ws(tab):
    settings = Settings()
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = retry(gc.open_by_key, DEST_ID)
    return sp, retry(sp.worksheet, tab)


def block_labels(col_a, lo, hi):
    m = {}
    for r in range(lo, hi):
        v = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
        if v and v not in m:
            m[v] = r
    return m


def restructure(tab):
    sp, ws = open_ws(tab)
    if ws.title == "DS-01 (在庫) ":
        print("DS-01は適用済み")
        return
    col_a = retry(ws.col_values, 1)
    blocks = oshima_tab_blocks_config.get_blocks(tab)
    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    ncols = ws.col_count
    END = cl(ncols)

    # ===== バックアップ =====
    os.makedirs("backups", exist_ok=True)
    bak = f"backups/{tab.replace('/', '_').strip()}_backup_{datetime.date.today()}.json"
    if not os.path.exists(bak):
        chunks = []
        step = 25
        for a in range(1, len(col_a) + 1, step):
            b = min(a + step - 1, len(col_a))
            g = retry(ws.get, f"A{a}:{END}{b}", value_render_option='FORMULA')
            g = g + [[]] * (b - a + 1 - len(g))
            chunks += g
        json.dump(chunks, open(bak, "w"), ensure_ascii=False)
        print(f"バックアップ: {bak}")
    else:
        chunks = json.load(open(bak))
        print("バックアップ既存 (再開)")

    # ===== ブロックごとの対象行を特定 =====
    del_rows = []      # 削除する絶対行
    per_block = []
    for bi, b in enumerate(blocks):
        lo, hi = bounds[bi], bounds[bi + 1]
        lab = block_labels(col_a, lo, hi)
        already = "販売価格" in lab and "amazon販売予想" not in lab
        info = {"lab": lab, "lo": lo, "hi": hi, "already": already}
        per_block.append(info)
        if already:
            continue
        for L in DELETE_LABELS:
            if L in lab:
                del_rows.append(lab[L])
    if not del_rows:
        print(f"[{tab}] 適用済みのためスキップ")
        return
    print(f"[{tab}] {len(blocks)}ブロック / 削除予定 {len(del_rows)}行")

    # ===== 参照付け替え (削除対象行を参照する全セル) =====
    target_map = {}   # 削除行 -> 付け替え先行 (Noneなら参照が無い想定)
    ev_rows = set()   # amazonイベント行 (条件除去用)
    for info in per_block:
        if info["already"]:
            continue
        lab = info["lab"]
        for L in DELETE_LABELS:
            if L not in lab:
                continue
            src = lab[L]
            if L == "amazonイベント":
                ev_rows.add(src)
                target_map[src] = None
            elif L in REPOINT and REPOINT[L] in lab:
                target_map[src] = lab[REPOINT[L]]
            else:
                target_map[src] = None

    pat = re.compile(r'(\$?[A-Z]{1,3}\$?)(' +
                     "|".join(str(r) for r in sorted(target_map)) +
                     r')(?![0-9])')
    n_fixed = 0
    for ri, row in enumerate(chunks, 1):
        if ri in target_map:
            continue
        need = False
        for v in row:
            if isinstance(v, str) and v.startswith("=") and pat.search(v):
                need = True
                break
        if not need:
            continue
        g = retry(ws.get, f"A{ri}:{END}{ri}", value_render_option='FORMULA')
        cur = g[0] if g else []
        out, changed = [], False
        for v in cur:
            if isinstance(v, str) and v.startswith("="):
                nv = v
                for er in ev_rows:
                    nv = nv.replace(f', $C${er}:${er}=""', "")
                    nv = nv.replace(f'$C${er}:${er}="", ', "")

                def sub(m):
                    tgt = target_map.get(int(m.group(2)))
                    return (m.group(1) + str(tgt)) if tgt else m.group(0)
                nv = pat.sub(sub, nv)
                if nv != v:
                    changed = True
                out.append(nv)
            else:
                out.append(v if v is not None else "")
        if changed:
            retry(ws.update, range_name=f"A{ri}:{cl(len(out))}{ri}",
                  values=[out], value_input_option='USER_ENTERED')
            n_fixed += 1
            print(f"  行{ri} 付け替え", flush=True)
    print(f"参照付け替え {n_fixed}行")

    # 残参照チェック (付け替え先が無いのに参照される行があれば中止)
    left = set()
    for ri, row in enumerate(chunks, 1):
        if ri in target_map:
            continue
        for v in row:
            if isinstance(v, str) and v.startswith("="):
                for m in pat.finditer(v):
                    if target_map.get(int(m.group(2))) is None and \
                            not any(f'$C${e}' in v for e in ev_rows):
                        pass  # イベント条件は除去済みのはず (再読で確認する)
    # 再読して最終確認
    bad = []
    for ri in sorted({r for r, row in enumerate(chunks, 1)
                      for v in row
                      if isinstance(v, str) and v.startswith("=")
                      and pat.search(v) and r not in target_map}):
        g = retry(ws.get, f"A{ri}:{END}{ri}", value_render_option='FORMULA')
        cur = g[0] if g else []
        for v in cur:
            if isinstance(v, str) and v.startswith("=") and pat.search(v):
                for m in pat.finditer(v):
                    if target_map.get(int(m.group(2))) is None:
                        bad.append((ri, str(v)[:80]))
                        break
    if bad:
        print("⚠️ 未解決の参照が残っています → 削除中止:", bad[:5])
        return

    # ===== 販売価格へ改名 + 全体の販売実績の未来ガード =====
    ups = []
    for info in per_block:
        if info["already"]:
            continue
        lab = info["lab"]
        if "セール価格" in lab:
            ups.append({"range": f"A{lab['セール価格']}",
                        "values": [["販売価格"]]})
        if "全体の販売実績" in lab:
            r = lab["全体の販売実績"]
            a1 = lab.get("amazonFBA販売実績")
            a2 = lab.get("楽天販売実績")
            a3 = lab.get("Yahoo販売実績")
            if a1 and a2 and a3:
                vals = []
                for c in range(3, ncols + 1):
                    L = cl(c)
                    vals.append(f'=IF({L}$1>TODAY(),"",'
                                f'SUM({L}{a1},{L}{a2},{L}{a3}))')
                retry(ws.update, range_name=f"C{r}:{END}{r}", values=[vals],
                      value_input_option='USER_ENTERED')
    if ups:
        retry(ws.batch_update, [dict(u) for u in ups],
              value_input_option='USER_ENTERED')
    print("販売価格改名 + 全体の販売実績ガード 完了")

    # ===== 行削除 =====
    reqs = [{"deleteDimension": {"range": {
        "sheetId": ws.id, "dimension": "ROWS",
        "startIndex": r - 1, "endIndex": r}}}
        for r in sorted(del_rows, reverse=True)]
    for i in range(0, len(reqs), 60):
        retry(sp.batch_update, {"requests": reqs[i:i + 60]})
    print(f"{len(del_rows)}行 削除完了")

    # ===== config 再生成 =====
    col_a = retry(ws.col_values, 1)
    starts = []
    for b in blocks:
        r = next((i for i, v in enumerate(col_a, 1)
                  if v.strip() == b["asin"]), None)
        assert r, f"{b['code']}: ASIN行が見つからない"
        starts.append(r)
    nb_bounds = starts + [len(col_a) + 2]
    KEYMAP = {"sales_row": "amazonFBA販売実績", "stock_row": "FBA在庫実績",
              "rakuten_sales_row": "楽天販売実績", "rsl_stock_row": "RSL在庫実績",
              "yahoo_sales_row": "Yahoo販売実績",
              "stock_crew_stock_row": "Stock Crew在庫実績",
              "change_qty_row": "在庫変更（数量）", "from_row": "FROM",
              "to_row": "TO",
              "fba_naganuma_row": "在庫予想長沼",
              "rsl_naganuma_row": "RSL在庫予想長沼",
              "sc_naganuma_row": "Stock Crew在庫予想長沼",
              "total_naganuma_row": "在庫総数予想長沼"}
    lines = [f'    # {datetime.date.today()}: DS-01方式に再構成 (元システム行削除・販売価格行)']
    lines.append(f'    "{tab}": [')
    for bi, b in enumerate(blocks):
        lo, hi = nb_bounds[bi], nb_bounds[bi + 1]
        lab = block_labels(col_a, lo, hi)
        code_r = next((i for i in range(lo, hi)
                       if col_a[i - 1].strip() == b["code"]), lo + 2)
        parts = [f'"code": "{b["code"]}"', f'"asin": "{b["asin"]}"',
                 f'"asin_row": {lo}', f'"code_row": {code_r}']
        for k, L in KEYMAP.items():
            if L in lab:
                parts.append(f'"{k}": {lab[L]}')
        lines.append("        {" + ", ".join(parts) + "},")
    lines.append("    ],")
    src = open("oshima_tab_blocks_config.py", encoding="utf-8").read()
    pat2 = re.compile(r'(?:^[ \t]*#[^\n]*\n)*^[ \t]*"' + re.escape(tab) +
                      r'": \[.*?^\s*\],', re.S | re.M)
    assert pat2.search(src)
    open("oshima_tab_blocks_config.py", "w", encoding="utf-8").write(
        pat2.sub(lambda _: "\n".join(lines), src, count=1))
    print("config 再生成完了")
    print(f"✅ [{tab}] 再構成完了")


def fill_prices(tab):
    from src.sp_client import SPClient
    settings = Settings()
    spc = SPClient(settings)
    sp, ws = open_ws(tab)
    col_a = retry(ws.col_values, 1)
    blocks = oshima_tab_blocks_config.get_blocks(tab)
    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    row1 = (retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
            or [[]])[0]
    c2d = {i: BASE + datetime.timedelta(days=int(v))
           for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    min_c, max_c = min(c2d), max(c2d)
    today = datetime.date.today()
    for bi, b in enumerate(blocks):
        lo, hi = bounds[bi], bounds[bi + 1]
        lab = block_labels(col_a, lo, hi)
        pr = lab.get("販売価格")
        if not pr:
            print(f"  {b['code']}: 販売価格行なし → スキップ")
            continue
        prices = {}
        cur = min(c2d.values())
        fail = 0
        while cur <= today:
            end = min(cur + datetime.timedelta(days=89), today)
            try:
                data = spc.get_order_metrics(
                    f"{cur.isoformat()}T00:00:00+09:00",
                    f"{end.isoformat()}T23:59:59+09:00",
                    granularity="Day", asin=b["asin"])
                for m in data:
                    d = datetime.date.fromisoformat(m.get("interval", "")[:10])
                    units = m.get("unitCount", 0)
                    avg = (m.get("averageUnitPrice") or {}).get("amount")
                    if units and avg:
                        prices[d] = float(avg)
            except Exception as e:
                fail += 1
                print(f"  {b['code']} {cur}: {str(e)[:80]}", flush=True)
            cur = end + datetime.timedelta(days=1)
            time.sleep(0.8)
        vals, last = [], ""
        for c in range(min_c, max_c + 1):
            d = c2d.get(c)
            if d is None or d > today:
                vals.append("")
                continue
            if d in prices:
                last = round(prices[d])
            vals.append(last if last != "" else "")
        retry(ws.update, range_name=f"{cl(min_c)}{pr}:{cl(max_c)}{pr}",
              values=[vals], value_input_option='USER_ENTERED')
        print(f"  {b['code']}: 販売価格 {len(prices)}日分 (失敗{fail})", flush=True)
    print(f"✅ [{tab}] 販売価格投入完了")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", required=True)
    ap.add_argument("--prices", action="store_true")
    args = ap.parse_args()
    if args.prices:
        fill_prices(args.tab)
    else:
        restructure(args.tab)


if __name__ == "__main__":
    main()
