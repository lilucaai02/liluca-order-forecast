#!/usr/bin/env python3
"""
大島コピーの各タブへ在庫実績を転記する。
  - FBA在庫実績 (stock_row):   日次Amazon在庫推移 (ASINごとに全アカウント合算)
  - RSL在庫実績 (rsl_stock_row): 日次楽天在庫推移 (正規化SKUで max 集約)
  - Stock Crew在庫実績 (stock_crew_stock_row): 日次Yahoo在庫推移 (正規化SKUで max 集約)

【安全装置】元シートのセルが空白/未定義/数値変換不可の場合は「不明」として扱い、
該当セルへの書き込みをスキップして転記先の既存値を保持する
（文字列/数値の "0" は取得成功した正常な0として区別し、他の値と同様に書き込む）。
複数アカウントが1つの値に合算/集約される場合、寄与する行のうち1つでも不明が
あればその(キー,日付)全体を不明として扱う（合算値の過小評価を防ぐため）。
元シートにそのSKUの行自体が無い場合も転記しない（そのチャネルで扱っていない
商品の既存値を 0 で塗り潰さないため）。

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
from typing import Dict, Set, Tuple

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


UNKNOWN = object()  # 取得失敗(空白/未定義/数値変換不可)を示すセンチネル。0とは区別する。


def _parse_qty(raw):
    """セルの生値をintに変換する。空文字列/None/数値変換不可はUNKNOWN(不明)を返す。
    文字列の"0"や数値0は取得成功した正常な0として扱う。"""
    if raw is None:
        return UNKNOWN
    if isinstance(raw, str) and raw.strip() == "":
        return UNKNOWN
    try:
        return int(raw)
    except (ValueError, TypeError):
        return UNKNOWN


def load_source(sp, sheet_name: str, dates: list, key_fn, agg: str
                 ) -> Tuple[Dict[Tuple[str, str], int], Set[Tuple[str, str]]]:
    """(既知の{(key, date): qty}, 不明な(key,date)の集合) を返す。agg='sum'|'max'。
    寄与する行のうち1つでも不明があれば、その(key,date)全体を不明として扱う。"""
    ws = retry(sp.worksheet, sheet_name)
    vals = retry(ws.get_all_values)
    hdr = vals[0]
    di = {}
    for i, v in enumerate(hdr):
        if v in dates:
            di[v] = i
    out: Dict[Tuple[str, str], int] = {}
    unknown: Set[Tuple[str, str]] = set()
    for row in vals[1:]:
        if len(row) < 2 or not row[0]:
            continue
        k = key_fn(row[0])
        for d, i in di.items():
            raw = row[i] if i < len(row) else None
            q = _parse_qty(raw)
            key = (k, d)
            if q is UNKNOWN:
                unknown.add(key)
                out.pop(key, None)
                continue
            if key in unknown:
                continue
            if agg == "sum":
                out[key] = out.get(key, 0) + q
            else:
                out[key] = max(out.get(key, q), q)
    return out, unknown


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

    amz, amz_unknown = load_source(src, "日次Amazon在庫推移", dates, lambda x: x, "sum")
    try:
        rsl, rsl_unknown = load_source(src, "日次楽天在庫推移", dates, normalize_sku, "max")
    except Exception as e:
        print(f"楽天在庫読み込みスキップ: {e}", file=sys.stderr)
        rsl, rsl_unknown = {}, set()
    try:
        sc, sc_unknown = load_source(src, "日次Yahoo在庫推移", dates, normalize_sku, "max")
    except Exception as e:
        print(f"Yahoo在庫読み込みスキップ: {e}", file=sys.stderr)
        sc, sc_unknown = {}, set()

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
    skip_log = []
    total_cells = 0
    for blk in blocks:
        for d in dates:
            c = d_to_c.get(d)
            if not c:
                continue
            # 在庫は「元シートに行があって値も取れた」ときだけ転記する。
            #   - 不明 (セルが空白)             → スキップ (既存値を保持)
            #   - 元シートにSKU行自体が無い     → スキップ (そのチャネルで扱っていない
            #                                     商品を 0 で塗り潰さないため)
            #   - 値が取れた                    → 0 でも転記する (実在庫0は正しい情報)
            for row_key, src, unknown_set, label in (
                ("stock_row", amz, amz_unknown, "FBA"),
                ("rsl_stock_row", rsl, rsl_unknown, "RSL"),
                ("stock_crew_stock_row", sc, sc_unknown, "SC"),
            ):
                if row_key not in blk:
                    continue
                total_cells += 1
                key = ((blk["asin"], d) if row_key == "stock_row"
                       else (normalize_sku(blk["code"]), d))
                if key in unknown_set:
                    skip_log.append(f"※ 元シートが空白(取得失敗の可能性)のため転記をスキップ: "
                                    f"{blk['code']} {label} / {d}")
                    continue
                if key not in src:
                    skip_log.append(f"※ 元シートに該当SKUの行が無いため転記をスキップ: "
                                    f"{blk['code']} {label} / {d}")
                    continue
                q = src[key]
                updates.append({"range": f"{col_letter(c)}{blk[row_key]}",
                                "values": [[q]]})
                log.append(f"  {blk['code']} {label} {d}: {q}")

    print(f"=== [大島コピー / {args.tab}] 在庫実績転記 ({dates[0]}〜{dates[-1]}) ===",
          file=sys.stderr)
    for line in log:
        print(line, file=sys.stderr)
    for line in skip_log:
        print(line, file=sys.stderr)
    if skip_log:
        print(f"※ 転記スキップ 合計 {len(skip_log)} セル（元シート未取得）", file=sys.stderr)

    if total_cells and len(skip_log) == total_cells:
        print(f"\nエラー: 全セル({total_cells}件)が元シート未取得(不明)のため転記できません",
              file=sys.stderr)
        sys.exit(1)

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
