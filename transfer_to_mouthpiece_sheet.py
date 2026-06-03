#!/usr/bin/env python3
"""
日次Amazon在庫推移シート → 発注予測スプレッドシート「マウスピース(在庫)」タブへ転記。

元シート（GOOGLE_SPREADSHEET_ID=12Di9y...）の「日次Amazon在庫推移」から
指定日のAmazon FBA在庫を読み、商品コードごとに合算して
転記先（1mbZla...）のマウスピース(在庫)タブの「FBA在庫実績」行に書き込む。

SKU正規化:
  小文字化、全角→半角括弧、(A) (A-2) (A-3) 等を除去して比較。
  例: "MP-02MHD（A）" → "mp-02mhd"

使い方:
  python3 transfer_to_mouthpiece_sheet.py                 # 今日
  python3 transfer_to_mouthpiece_sheet.py --date 2026-06-03
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings


# ===== 設定 =====
DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
DEST_SHEET_NAME = "マウスピース(在庫)"
SRC_SHEET_NAME = "日次Amazon在庫推移"

# マウスピース(在庫)タブの商品ブロック → FBA在庫実績行（または在庫実績行）の行番号
BLOCK_MAPPING: List[Tuple[str, int]] = [
    ("MP-02MHD",        18),
    ("MP-02MHD6",       70),
    ("MP-03",          121),
    ("MP-01",          173),
    ("MP-02",          225),
    ("MP-02MHD-small", 277),
    ("MP-04",          328),
    ("MP-01-MHP",      370),  # 異なるレイアウト、「在庫実績」行
]

# Excel/Google Sheets シリアル日付の起点
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def normalize_sku(sku: str) -> str:
    """SKU を正規化: 小文字化、全角→半角括弧、(...) 除去。"""
    s = sku.lower().strip()
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\([^)]*\)", "", s)
    return s.strip()


def col_letter(n: int) -> str:
    """1始まりの列番号を A, B, ..., Z, AA, AB, ... に変換。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_source_inventory(gc, src_spreadsheet_id: str, date_str: str) -> Dict[str, int]:
    """
    元シートから指定日のAmazon在庫を読み、{正規化SKU: 合算qty} を返す。
    複数アカウントは合算。
    """
    sp = gc.open_by_key(src_spreadsheet_id)
    ws = sp.worksheet(SRC_SHEET_NAME)

    # 1行目から日付列を探す
    row1 = ws.row_values(1)
    target_col_idx = None
    for i, v in enumerate(row1, start=1):
        if v == date_str:
            target_col_idx = i
            break
    if target_col_idx is None:
        raise ValueError(f"元シート1行目に日付 '{date_str}' が見つかりません")

    # A列(SKU), B列(アカウント), 該当日列 を読む
    col_a = ws.col_values(1)
    n = len(col_a)
    target_col_letter = col_letter(target_col_idx)
    rng = f"A2:{target_col_letter}{n}"
    rows = ws.get(rng)

    aggregated: Dict[str, int] = {}
    for row in rows:
        if len(row) < 1:
            continue
        sku = row[0]
        if not sku:
            continue
        # 該当日列の値
        if len(row) >= target_col_idx:
            try:
                qty = int(row[target_col_idx - 1] or 0)
            except (ValueError, TypeError):
                qty = 0
        else:
            qty = 0
        key = normalize_sku(sku)
        aggregated[key] = aggregated.get(key, 0) + qty
    return aggregated


def find_date_column_in_dest(ws, date_str: str) -> int:
    """転記先タブ1行目（シリアル日付）から日付列を探す。"""
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    target_serial = (target - SERIAL_DATE_BASE).days

    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) == target_serial:
            return i
    raise ValueError(f"転記先タブ1行目に日付 '{date_str}' (serial={target_serial}) が見つかりません")


def aggregate_for_blocks(by_norm_sku: Dict[str, int]) -> Dict[str, int]:
    """商品コードごとに、正規化SKUと完全一致する分の合計を返す。"""
    result: Dict[str, int] = {}
    for code, _row in BLOCK_MAPPING:
        norm_code = normalize_sku(code)
        result[code] = by_norm_sku.get(norm_code, 0)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",
                        default=datetime.date.today().strftime("%Y-%m-%d"),
                        help="記録日付 (YYYY-MM-DD)。デフォルト: 今日")
    parser.add_argument("--dry-run", action="store_true",
                        help="書き込みせず、集計結果のみ表示")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .envに GOOGLE_CREDENTIALS_FILE と GOOGLE_SPREADSHEET_ID を設定してください", file=sys.stderr)
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

    print(f"=== マウスピース(在庫)タブ転記 [{args.date}] ===", file=sys.stderr)

    # 1. 元シートから今日のAmazon在庫を取得
    by_norm = read_source_inventory(gc, settings.google_spreadsheet_id, args.date)
    print(f"元シート: {len(by_norm)}個の正規化SKUを集計", file=sys.stderr)

    # 2. ブロックごとに合算
    block_totals = aggregate_for_blocks(by_norm)
    print(f"\n=== 商品コードごとの集計 ===", file=sys.stderr)
    for code, total in block_totals.items():
        print(f"  {code}: {total}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    # 3. 転記先タブの日付列を特定
    dest_sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    dest_ws = dest_sp.worksheet(DEST_SHEET_NAME)
    date_col_idx = find_date_column_in_dest(dest_ws, args.date)
    date_col = col_letter(date_col_idx)
    print(f"\n転記先 {DEST_SHEET_NAME} の {args.date} = {date_col}列", file=sys.stderr)

    # 4. 一括書き込み（batch_update）
    updates = []
    for code, row in BLOCK_MAPPING:
        cell = f"{date_col}{row}"
        updates.append({"range": cell, "values": [[block_totals[code]]]})

    dest_ws.batch_update(updates, value_input_option='USER_ENTERED')
    print(f"\n→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
