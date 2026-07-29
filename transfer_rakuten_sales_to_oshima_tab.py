#!/usr/bin/env python3
"""
日次楽天販売推移シート → 大島コピー各タブの「楽天販売実績」行への転記。

転記先: 発注予測 のコピー20260722大島 (1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU)
タブ・ブロック定義: oshima_tab_blocks_config.OSHIMA_TAB_BLOCKS

使い方:
  python3 transfer_rakuten_sales_to_oshima_tab.py --tab "DS-01 (在庫) "
  python3 transfer_rakuten_sales_to_oshima_tab.py --tab "マウスピース(在庫)" --dry-run

【安全装置】元シートのセルが空白/未定義/数値変換不可の場合は「不明」として扱い、
該当(商品コード,日付)への書き込みをスキップして転記先の既存値を保持する
（文字列/数値の "0" は取得成功した正常な0として区別する）。
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from oshima_tab_blocks_config import get_blocks

DEST_SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
SRC_SHEET_NAME = "日次楽天販売推移"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def normalize_sku(sku: str) -> str:
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


def read_sales_from_source(gc, src_spreadsheet_id: str,
                           dates: List[str]
                           ) -> Tuple[Dict[Tuple[str, str], int], Set[Tuple[str, str]]]:
    """戻り値は (既知の合算qty, 不明な(正規化SKU,日付)の集合)。
    寄与する行のうち1つでも不明があれば、その(正規化SKU,日付)全体を不明として扱う。"""
    sp = gc.open_by_key(src_spreadsheet_id)
    ws = sp.worksheet(SRC_SHEET_NAME)

    row1 = ws.row_values(1)
    date_to_col: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if v in dates:
            date_to_col[v] = i

    missing = [d for d in dates if d not in date_to_col]
    if missing:
        raise ValueError(f"元シートに日付列がありません: {missing}")

    last_col = max(date_to_col.values())
    last_col_letter = col_letter(last_col)
    all_data = ws.get(f"A2:{last_col_letter}")

    result: Dict[Tuple[str, str], int] = {}
    unknown_keys: Set[Tuple[str, str]] = set()
    for row in all_data:
        if len(row) < 1:
            continue
        sku = row[0]
        if not sku:
            continue
        norm = normalize_sku(sku)
        for date_str, col_idx in date_to_col.items():
            raw = row[col_idx - 1] if len(row) >= col_idx else None
            qty = _parse_qty(raw)
            key = (norm, date_str)
            if qty is UNKNOWN:
                unknown_keys.add(key)
                result.pop(key, None)
                continue
            if key in unknown_keys:
                continue
            result[key] = result.get(key, 0) + qty
    return result, unknown_keys


def find_date_columns_bulk(ws, date_strs: List[str]) -> Dict[str, int]:
    """1回の read で複数日付の列位置をまとめて解決 (quota節約)。"""
    row1_raw = ws.get('1:1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    serial_to_col: Dict[int, int] = {}
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)):
            serial_to_col[int(v)] = i
    result: Dict[str, int] = {}
    missing = []
    for d in date_strs:
        target = datetime.datetime.strptime(d, "%Y-%m-%d").date()
        serial = (target - SERIAL_DATE_BASE).days
        if serial in serial_to_col:
            result[d] = serial_to_col[serial]
        else:
            missing.append((d, serial))
    if missing:
        raise ValueError(f"転記先タブに日付列が見つかりません: {missing}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", required=True)
    today = datetime.date.today()
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=5)).strftime("%Y-%m-%d"))
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    blocks = get_blocks(args.tab)
    blocks_with_rakuten = [b for b in blocks if "rakuten_sales_row" in b]
    if not blocks_with_rakuten:
        print(f"⚠ {args.tab} に rakuten_sales_row 定義のブロックがありません",
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

    from_d = datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_d = datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date()
    dates: List[str] = []
    d = from_d
    while d <= to_d:
        dates.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)

    print(f"=== [大島コピー / {args.tab}] 楽天販売実績 転記 "
          f"[{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)

    sales, sales_unknown = read_sales_from_source(gc, settings.google_spreadsheet_id, dates)
    print(f"元シートから {len(sales)}件の (正規化SKU,日付) レコード"
          f"（不明 {len(sales_unknown)}件）", file=sys.stderr)

    by_code_date: Dict[Tuple[str, str], int] = {}
    by_code_date_unknown: Set[Tuple[str, str]] = set()
    print(f"\n=== 商品コード×日付 集計 ===", file=sys.stderr)
    for blk in blocks_with_rakuten:
        norm_code = normalize_sku(blk["code"])
        for d_str in dates:
            src_key = (norm_code, d_str)
            dest_key = (blk["code"], d_str)
            if src_key in sales_unknown:
                by_code_date_unknown.add(dest_key)
                print(f"  {blk['code']} / {d_str}: 不明(取得失敗の可能性)", file=sys.stderr)
                continue
            qty = sales.get(src_key, 0)
            by_code_date[dest_key] = qty
            print(f"  {blk['code']} / {d_str}: {qty}", file=sys.stderr)

    total_cells = len(blocks_with_rakuten) * len(dates)
    if by_code_date_unknown and len(by_code_date_unknown) == total_cells:
        print(f"\nエラー: 全セル({total_cells}件)が元シート未取得(不明)のため転記できません",
              file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(args.tab)
    date_to_col_dest: Dict[str, int] = find_date_columns_bulk(dest_ws, dates)

    updates = []
    skipped = 0
    for blk in blocks_with_rakuten:
        for d_str in dates:
            key = (blk["code"], d_str)
            if key in by_code_date_unknown:
                skipped += 1
                print(f"※ 元シートが空白(取得失敗の可能性)のため転記をスキップ: "
                      f"{blk['code']} / {d_str}", file=sys.stderr)
                continue
            qty = by_code_date.get(key, 0)
            col = col_letter(date_to_col_dest[d_str])
            updates.append({"range": f"{col}{blk['rakuten_sales_row']}",
                            "values": [[qty]]})

    if skipped:
        print(f"※ 転記スキップ 合計 {skipped} セル（元シート未取得）", file=sys.stderr)

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
