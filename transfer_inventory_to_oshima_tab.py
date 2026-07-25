#!/usr/bin/env python3
"""
大島コピーの各タブへ在庫実績を転記する。
  - FBA在庫実績 (stock_row):   日次Amazon在庫推移 (ASINごとに全アカウント合算)
  - RSL在庫実績 (rsl_stock_row): 日次楽天在庫推移 (正規化SKUで max 集約)
  - Stock Crew在庫実績 (stock_crew_stock_row): 日次Yahoo在庫推移 (正規化SKUで max 集約)

値が 0 の日（取得失敗の可能性）は書き込まずスキップする。

使い方:
  python3 transfer_inventory_to_oshima_tab.py --tab "DS-01 (在庫) "
  python3 transfer_inventory_to_oshima_tab.py --tab "TG-01(在庫)" --days 5
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
from typing import Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from oshima_tab_blocks_config import get_blocks

DEST_SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)


def retry(fn, *a, **k):
    from gspread.exceptions import APIError
    delay = 30
    for i in range(6):
        try:
            return fn(*a, **k)
        except APIError as e:
            if "429" in str(e) and i < 5:
                print(f"  [quota] {delay}s待機...", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                raise


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def normalize_sku(sku: str) -> str:
    s = sku.lower().strip().replace("（", "(").replace("）", ")")
    s = re.sub(r"\([^)]*\)", "", s)
    if ":" in s:
        s = s.split(":", 1)[1]
    return s.strip()


def load_source(sp, sheet_name: str, dates: list, key_fn, agg: str):
    """{(key, date): qty} を返す。agg='sum'|'max'"""
    ws = retry(sp.worksheet, sheet_name)
    vals = retry(ws.get_all_values)
    hdr = vals[0]
    di = {}
    for i, v in enumerate(hdr):
        if v in dates:
            di[v] = i
    out: Dict[Tuple[str, str], int] = {}
    for row in vals[1:]:
        if len(row) < 2 or not row[0]:
            continue
        k = key_fn(row[0])
        for d, i in di.items():
            v = row[i] if i < len(row) else ""
            if not v:
                continue
            try:
                q = int(v)
            except ValueError:
                continue
            key = (k, d)
            if agg == "sum":
                out[key] = out.get(key, 0) + q
            else:
                out[key] = max(out.get(key, q), q)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", required=True)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = Settings()
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)

    today = datetime.date.today()
    dates = [(today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(args.days, 0, -1)]

    blocks = get_blocks(args.tab)
    src = retry(gc.open_by_key, settings.google_spreadsheet_id)

    amz = load_source(src, "日次Amazon在庫推移", dates, lambda x: x, "sum")
    try:
        rsl = load_source(src, "日次楽天在庫推移", dates, normalize_sku, "max")
    except Exception as e:
        print(f"楽天在庫読み込みスキップ: {e}", file=sys.stderr)
        rsl = {}
    try:
        sc = load_source(src, "日次Yahoo在庫推移", dates, normalize_sku, "max")
    except Exception as e:
        print(f"Yahoo在庫読み込みスキップ: {e}", file=sys.stderr)
        sc = {}

    dest = retry(gc.open_by_key, DEST_SPREADSHEET_ID)
    ws = retry(dest.worksheet, args.tab)
    row1_raw = retry(ws.get, '1:1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    d_to_c = {}
    for i, v in enumerate(row1, 1):
        if isinstance(v, (int, float)):
            d_to_c[(BASE + datetime.timedelta(days=int(v))).strftime("%Y-%m-%d")] = i

    updates = []
    log = []
    for blk in blocks:
        for d in dates:
            c = d_to_c.get(d)
            if not c:
                continue
            # FBA (ASIN合算)
            q = amz.get((blk["asin"], d), 0)
            if q > 0 and "stock_row" in blk:
                updates.append({"range": f"{col_letter(c)}{blk['stock_row']}",
                                "values": [[q]]})
                log.append(f"  {blk['code']} FBA {d}: {q}")
            # RSL (正規化コード)
            q2 = rsl.get((normalize_sku(blk["code"]), d), 0)
            if q2 > 0 and "rsl_stock_row" in blk:
                updates.append({"range": f"{col_letter(c)}{blk['rsl_stock_row']}",
                                "values": [[q2]]})
                log.append(f"  {blk['code']} RSL {d}: {q2}")
            # Stock Crew = Yahoo在庫 (正規化コード)
            q3 = sc.get((normalize_sku(blk["code"]), d), 0)
            if q3 > 0 and "stock_crew_stock_row" in blk:
                updates.append({
                    "range": f"{col_letter(c)}{blk['stock_crew_stock_row']}",
                    "values": [[q3]]})
                log.append(f"  {blk['code']} SC {d}: {q3}")

    print(f"=== [大島コピー / {args.tab}] 在庫実績転記 ({dates[0]}〜{dates[-1]}) ===",
          file=sys.stderr)
    for line in log:
        print(line, file=sys.stderr)
    if args.dry_run:
        print(f"[dry-run] {len(updates)}セル 書き込みスキップ", file=sys.stderr)
        return
    if updates:
        BATCH = 100
        for i in range(0, len(updates), BATCH):
            retry(ws.batch_update, [dict(u) for u in updates[i:i+BATCH]],
                  value_input_option='USER_ENTERED')
    print(f"→ {len(updates)}セル書き込み完了", file=sys.stderr)


if __name__ == "__main__":
    main()
