#!/usr/bin/env python3
"""
日次Yahoo在庫推移シート → 各タブの「Stock Crew在庫実績」行への転記（汎用）。

タブ別の設定は tab_blocks_config.py の TAB_BLOCKS で管理。
ブロックに `stock_crew_stock_row` が定義されていない場合はスキップ。

使い方:
  python3 transfer_yahoo_inventory_to_tab.py --tab "DS-01 (在庫) "
  python3 transfer_yahoo_inventory_to_tab.py --tab "マウスピース(在庫)" --date 2026-07-22

【安全装置】元シートのセルが空白/未定義/数値変換不可の場合は「不明」として扱い、
該当(商品コード)への書き込みをスキップして転記先の既存値を保持する
（文字列/数値の "0" は取得成功した正常な0として区別する）。
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import get_blocks

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SRC_SHEET_NAME = "日次Yahoo在庫推移"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def normalize_sku(sku: str) -> str:
    """Yahoo SKU を商品コードと一致するように正規化（sub_code 部分を抽出）。"""
    s = sku.lower().strip()
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\([^)]*\)", "", s)
    if ":" in s:
        s = s.split(":", 1)[1]
    return s.strip()


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


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


def read_source_inventory(gc, src_spreadsheet_id: str, date_str: str
                           ) -> Tuple[Dict[str, int], Set[str]]:
    """(既知の{正規化SKU: 合算qty}, 不明な正規化SKUの集合) を返す。
    寄与する行のうち1つでも不明があれば、その正規化SKU全体を不明として扱う。"""
    sp = gc.open_by_key(src_spreadsheet_id)
    ws = sp.worksheet(SRC_SHEET_NAME)

    row1 = ws.row_values(1)
    target_col_idx = None
    for i, v in enumerate(row1, start=1):
        if v == date_str:
            target_col_idx = i
            break
    if target_col_idx is None:
        raise ValueError(f"元シート1行目に日付 '{date_str}' が見つかりません")

    col_a = ws.col_values(1)
    n = len(col_a)
    target_col_letter = col_letter(target_col_idx)
    rows = ws.get(f"A2:{target_col_letter}{n}")

    aggregated: Dict[str, int] = {}
    unknown: Set[str] = set()
    for row in rows:
        if len(row) < 1:
            continue
        sku = row[0]
        if not sku:
            continue
        raw = row[target_col_idx - 1] if len(row) >= target_col_idx else None
        qty = _parse_qty(raw)
        key = normalize_sku(sku)
        if qty is UNKNOWN:
            unknown.add(key)
            aggregated.pop(key, None)
            continue
        if key in unknown:
            continue
        # 合算時: -1(無制限) と正数が混じった場合は max（正数）を優先
        if key in aggregated:
            aggregated[key] = max(aggregated[key], qty)
        else:
            aggregated[key] = qty
    return aggregated, unknown


def find_date_column_in_dest(ws, date_str: str) -> int:
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    target_serial = (target - SERIAL_DATE_BASE).days
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) == target_serial:
            return i
    raise ValueError(f"転記先タブに日付 '{date_str}' (serial={target_serial}) が見つかりません")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", required=True)
    parser.add_argument("--date",
                        default=datetime.date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    blocks = get_blocks(args.tab)
    blocks_with_stock_crew = [b for b in blocks if "stock_crew_stock_row" in b]
    if not blocks_with_stock_crew:
        print(f"⚠ {args.tab} に stock_crew_stock_row 定義のブロックがありません",
              file=sys.stderr)
        return

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)

    print(f"=== [{args.tab}] Stock Crew在庫実績 転記 [{args.date}] ===", file=sys.stderr)

    by_norm, by_norm_unknown = read_source_inventory(gc, settings.google_spreadsheet_id, args.date)
    print(f"元シート: {len(by_norm)}個の正規化SKU（不明 {len(by_norm_unknown)}個）",
          file=sys.stderr)

    print(f"\n=== 商品コードごとの集計 ===", file=sys.stderr)
    block_totals: Dict[str, int] = {}
    unknown_codes = []
    for blk in blocks_with_stock_crew:
        norm_code = normalize_sku(blk["code"])
        if norm_code in by_norm_unknown:
            unknown_codes.append(blk["code"])
            print(f"  {blk['code']}: 不明(取得失敗の可能性)", file=sys.stderr)
            continue
        total = by_norm.get(norm_code, 0)
        block_totals[blk["code"]] = total
        print(f"  {blk['code']}: {total}", file=sys.stderr)

    if unknown_codes and len(unknown_codes) == len(blocks_with_stock_crew):
        print(f"\nエラー: 全セル({len(blocks_with_stock_crew)}件)が元シート未取得(不明)のため転記できません",
              file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(args.tab)
    date_col_idx = find_date_column_in_dest(dest_ws, args.date)
    date_col = col_letter(date_col_idx)
    print(f"\n転記先 {args.tab} の {args.date} = {date_col}列", file=sys.stderr)

    updates = []
    for blk in blocks_with_stock_crew:
        if blk["code"] in unknown_codes:
            print(f"※ 元シートが空白(取得失敗の可能性)のため転記をスキップ: "
                  f"{blk['code']} / {args.date}", file=sys.stderr)
            continue
        cell = f"{date_col}{blk['stock_crew_stock_row']}"
        updates.append({"range": cell, "values": [[block_totals[blk["code"]]]]})

    if unknown_codes:
        print(f"※ 転記スキップ 合計 {len(unknown_codes)} セル（元シート未取得）", file=sys.stderr)

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
