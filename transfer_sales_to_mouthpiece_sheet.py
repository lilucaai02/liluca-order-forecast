#!/usr/bin/env python3
"""
日次Amazon販売推移シート → 発注予測スプレッドシートの「マウスピース(在庫)」タブの
「amazonFBA販売実績」行への転記。

ASIN → 商品コード のマッピングはマウスピース(在庫)タブの R1（ASIN）と
R3 または R+1（商品コード）から自動取得する。

使い方:
  python3 transfer_sales_to_mouthpiece_sheet.py                     # 昨日と一昨日
  python3 transfer_sales_to_mouthpiece_sheet.py --from-date 2026-06-01 --to-date 2026-06-02
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
DEST_SHEET_NAME = "マウスピース(在庫)"
SRC_SHEET_NAME = "日次Amazon販売推移"

# (商品コード, amazonFBA販売実績の行番号)
BLOCK_MAPPING: List[Tuple[str, int]] = [
    ("MP-02MHD",         10),
    ("MP-02MHD6",        62),
    ("MP-03",           113),
    ("MP-01",           165),
    ("MP-02",           217),
    ("MP-02MHD-small", 269),
    ("MP-04",          320),
]

# マウスピース(在庫)タブの各ブロックのASIN行と商品コード行
# ブロック1だけ R1=ASIN, R3=商品コード（R2は「週」行）
# 他は ASIN行=ブロック開始, 商品コード行=ASIN行+1
ASIN_ROW_MAPPING: List[Tuple[int, int]] = [
    (1,   3),   # MP-02MHD
    (54,  55),  # MP-02MHD6
    (105, 106), # MP-03
    (157, 158), # MP-01
    (209, 210), # MP-02
    (261, 262), # MP-02MHD-small
    (312, 313), # MP-04
]

SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def get_asin_to_code_mapping(ws_dest) -> Dict[str, str]:
    """マウスピース(在庫)タブから ASIN → 商品コードマッピングを取得。"""
    mapping: Dict[str, str] = {}
    cells = []
    for asin_row, code_row in ASIN_ROW_MAPPING:
        cells.append(f"A{asin_row}")
        cells.append(f"A{code_row}")
    # batch_get で一度に取得
    ranges = [f"A{r}" for pair in ASIN_ROW_MAPPING for r in pair]
    result = ws_dest.batch_get(ranges)
    # ranges は [asin1, code1, asin2, code2, ...]
    for i in range(0, len(result), 2):
        asin_v = result[i]
        code_v = result[i+1]
        asin = asin_v[0][0] if asin_v and asin_v[0] else ""
        code = code_v[0][0] if code_v and code_v[0] else ""
        if asin and code:
            mapping[asin] = code
    return mapping


def read_sales_from_source(gc, src_spreadsheet_id: str,
                           dates: List[str]) -> Dict[Tuple[str, str], int]:
    """
    日次Amazon販売推移シートから (ASIN, 日付) → 全アカウント合算 qty を返す。
    """
    sp = gc.open_by_key(src_spreadsheet_id)
    ws = sp.worksheet(SRC_SHEET_NAME)

    row1 = ws.row_values(1)
    # 各日付の列番号
    date_to_col: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if v in dates:
            date_to_col[v] = i

    missing = [d for d in dates if d not in date_to_col]
    if missing:
        raise ValueError(f"元シートに日付列がありません: {missing}")

    # A列 (ASIN), B列 (アカウント), C列(対応SKU), 各日付列を読む
    # 列構成変更: 日付列が D 列以降に移動した
    last_col = max(date_to_col.values())
    last_col_letter = col_letter(last_col)
    all_data = ws.get(f"A2:{last_col_letter}")

    result: Dict[Tuple[str, str], int] = {}
    for row in all_data:
        if len(row) < 2:
            continue
        asin = row[0]
        if not asin:
            continue
        for date_str, col_idx in date_to_col.items():
            if len(row) >= col_idx:
                try:
                    qty = int(row[col_idx - 1] or 0)
                except (ValueError, TypeError):
                    qty = 0
            else:
                qty = 0
            key = (asin, date_str)
            result[key] = result.get(key, 0) + qty
    return result


def find_date_column(ws, date_str: str) -> int:
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
    today = datetime.date.today()
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=2)).strftime("%Y-%m-%d"))
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

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

    # 日付リスト
    from_d = datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_d = datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date()
    dates: List[str] = []
    d = from_d
    while d <= to_d:
        dates.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)

    print(f"=== マウスピース(在庫) amazonFBA販売実績 転記 [{args.from_date} ～ {args.to_date}] ===",
          file=sys.stderr)

    # 1. 転記先タブから ASIN→商品コードマッピングを取得
    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(DEST_SHEET_NAME)
    asin_to_code = get_asin_to_code_mapping(dest_ws)
    print(f"ASIN→商品コード マッピング: {len(asin_to_code)}件", file=sys.stderr)
    for asin, code in asin_to_code.items():
        print(f"  {asin} → {code}", file=sys.stderr)

    # 2. 元シートから販売数を取得（ASIN×日付）
    sales = read_sales_from_source(gc, settings.google_spreadsheet_id, dates)
    print(f"\n元シートから {len(sales)}件の (ASIN,日付) 販売レコードを取得", file=sys.stderr)

    # 3. 商品コード×日付ごとに集計
    code_to_row = {code: row for code, row in BLOCK_MAPPING}
    by_code: Dict[Tuple[str, str], int] = {}
    for (asin, date_str), qty in sales.items():
        code = asin_to_code.get(asin)
        if code is None or code not in code_to_row:
            continue
        by_code[(code, date_str)] = by_code.get((code, date_str), 0) + qty

    print(f"\n=== 商品コード×日付 集計 ===", file=sys.stderr)
    for (code, date_str), qty in sorted(by_code.items()):
        print(f"  {code} / {date_str}: {qty}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    # 4. 転記先タブの各日付の列番号を取得
    date_to_col_dest: Dict[str, int] = {}
    for d_str in dates:
        date_to_col_dest[d_str] = find_date_column(dest_ws, d_str)

    # 5. 書き込み
    updates = []
    for code, row in BLOCK_MAPPING:
        for d_str in dates:
            qty = by_code.get((code, d_str), 0)
            col = col_letter(date_to_col_dest[d_str])
            updates.append({"range": f"{col}{row}", "values": [[qty]]})

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
