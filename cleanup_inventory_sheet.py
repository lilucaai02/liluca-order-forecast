#!/usr/bin/env python3
"""
日次Amazon在庫推移シートの整理:
  1. SKU 行 → ASIN に変換（販売推移シートの C列「対応SKU」から逆引き辞書）
  2. 同じ (ASIN, アカウント) の複数行を日付ごとに max で集約
  3. 変換不可行は「日次Amazon在庫推移_未対応SKU」タブへ退避
  4. 元シートは「日次Amazon在庫推移_バックアップYYYYMMDD_HHMMSS」にコピー

行構造の統一（販売推移と同じ）:
  A=ASIN, B=アカウント, C=対応SKU (カンマ区切り), D以降=各日付の在庫数

使い方:
  python3 cleanup_inventory_sheet.py --dry-run    # 変換予定を表示のみ
  python3 cleanup_inventory_sheet.py --apply      # 実際に書き換え
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

SRC_ID = "12Di9y6pwb7CI39GKpR9QPNKCtCqEilTNGjDXVUmjq6c"
SALES_SHEET = "日次Amazon販売推移"
INV_SHEET = "日次Amazon在庫推移"

ASIN_RE = re.compile(r'^B0[A-Z0-9]{8}$')


def normalize_sku(s: str) -> str:
    """SKU を正規化: 小文字化・半角化・(...) 除去・空白除去。"""
    s = s.lower().strip()
    s = s.replace("（", "(").replace("）", ")")
    s = s.translate(str.maketrans(
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９",
        "abcdefghijklmnopqrstuvwxyz0123456789"))
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def build_sku_to_asin(gc) -> Dict[Tuple[str, str], str]:
    """販売推移シートから (正規化SKU, アカウント) → ASIN の辞書を作る。"""
    sp = gc.open_by_key(SRC_ID)
    ws = sp.worksheet(SALES_SHEET)
    rows = ws.get('A2:C500')
    m: Dict[Tuple[str, str], str] = {}
    for row in rows:
        if len(row) < 3:
            continue
        asin, acc, skus_str = row[0], row[1], row[2]
        if not (asin and acc):
            continue
        for sku in skus_str.split(","):
            n = normalize_sku(sku)
            if n:
                m[(n, acc)] = asin
    return m


def get_last_col_letter(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def process(dry_run: bool):
    settings = Settings()
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

    sp = gc.open_by_key(SRC_ID)
    ws_inv = sp.worksheet(INV_SHEET)

    # 逆引き辞書
    sku_to_asin = build_sku_to_asin(gc)
    print(f"逆引き辞書サイズ (販売推移C列から): {len(sku_to_asin)}", file=sys.stderr)

    # 在庫推移シート全体を読む
    all_data = ws_inv.get_all_values()
    if not all_data:
        print("エラー: シートが空", file=sys.stderr)
        return

    header = all_data[0]
    body = all_data[1:]
    n_cols = len(header)
    print(f"元シート: {len(body)}データ行, {n_cols}列", file=sys.stderr)

    # 日付列: D列以降 (index 3 以降)
    date_columns = header[3:]
    print(f"日付列数: {len(date_columns)}", file=sys.stderr)

    # 集約: (ASIN, アカウント) → {日付idx → max(qty)}
    aggregated: Dict[Tuple[str, str], Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    # (ASIN, アカウント) → 対応SKUのセット (元がSKU形式だった場合のみ)
    sku_map: Dict[Tuple[str, str], set] = defaultdict(set)

    unmatched_rows = []  # 変換不可の行 (row そのまま保存)
    matched_asin_count = 0
    matched_sku_count = 0

    for row in body:
        # 末尾のセルが欠けていたら空扱いで埋める
        row = row + [""] * (n_cols - len(row))
        raw_a, acc = row[0].strip(), row[1].strip()
        existing_c = row[2].strip() if len(row) > 2 else ""
        if not raw_a or not acc:
            continue

        # ASIN か SKU か判定
        if ASIN_RE.match(raw_a):
            asin = raw_a
            key = (asin, acc)
            matched_asin_count += 1
            # 既存の C列 SKU も引き継ぐ
            if existing_c:
                for s in existing_c.split(","):
                    if s.strip():
                        sku_map[key].add(s.strip())
        else:
            # SKU → 逆引き
            n = normalize_sku(raw_a)
            asin = sku_to_asin.get((n, acc))
            if not asin:
                unmatched_rows.append(row)
                continue
            key = (asin, acc)
            matched_sku_count += 1
            sku_map[key].add(raw_a)

        # 各日付列を max で集約
        for i, val in enumerate(row[3:]):
            if not val:
                continue
            try:
                q = int(str(val).replace(",", "").strip())
            except (ValueError, TypeError):
                continue
            if aggregated[key][i] < q:
                aggregated[key][i] = q

    print(f"\n=== 集約結果 ===", file=sys.stderr)
    print(f"  ASIN行そのまま:    {matched_asin_count}", file=sys.stderr)
    print(f"  SKU→ASIN変換成功:  {matched_sku_count}", file=sys.stderr)
    print(f"  変換不可(退避):    {len(unmatched_rows)}", file=sys.stderr)
    print(f"  ユニーク(ASIN,acc): {len(aggregated)}", file=sys.stderr)

    # ソート: アカウント → ASIN の順
    sorted_keys = sorted(aggregated.keys(), key=lambda k: (k[1], k[0]))

    # 新しいシート内容を組み立て
    new_header = ["ASIN", "アカウント", "対応SKU"] + date_columns
    new_body = []
    for asin, acc in sorted_keys:
        skus = sorted(sku_map[(asin, acc)])
        row = [asin, acc, ", ".join(skus)]
        for i in range(len(date_columns)):
            v = aggregated[(asin, acc)].get(i)
            row.append(str(v) if v is not None else "")
        new_body.append(row)

    print(f"\n=== 出力予定 ===", file=sys.stderr)
    print(f"  新body行数: {len(new_body)}", file=sys.stderr)
    print(f"  未対応SKU退避: {len(unmatched_rows)}行", file=sys.stderr)
    if unmatched_rows:
        print(f"\n未対応SKU 先頭5行:", file=sys.stderr)
        for r in unmatched_rows[:5]:
            print(f"  {r[:4]}", file=sys.stderr)

    if dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    # === 本実行 ===
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{INV_SHEET}_バックアップ{ts}"
    print(f"\n1) バックアップ作成: '{backup_name}'", file=sys.stderr)
    ws_backup = ws_inv.duplicate(new_sheet_name=backup_name)
    print(f"   完了 (gid={ws_backup.id})", file=sys.stderr)

    # 2) 本タブ全消去 → 新内容書き込み
    print(f"2) 本タブ '{INV_SHEET}' をクリアして書き込み", file=sys.stderr)
    ws_inv.clear()
    last_col_idx = 3 + len(date_columns)
    last_col = get_last_col_letter(last_col_idx)
    values = [new_header] + new_body
    end_row = len(values)
    ws_inv.update(range_name=f"A1:{last_col}{end_row}",
                  values=values, value_input_option='USER_ENTERED')
    ws_inv.freeze(rows=1, cols=3)
    print(f"   {end_row}行 × {last_col_idx}列 書き込み完了", file=sys.stderr)

    # 3) 未対応SKU タブに退避
    if unmatched_rows:
        unmatched_name = f"{INV_SHEET}_未対応SKU"
        print(f"3) 未対応SKU退避先: '{unmatched_name}'", file=sys.stderr)
        try:
            ws_un = sp.worksheet(unmatched_name)
            ws_un.clear()
        except Exception:
            ws_un = sp.add_worksheet(title=unmatched_name,
                                     rows=max(len(unmatched_rows) + 10, 100),
                                     cols=n_cols)
        un_values = [header] + [r[:n_cols] for r in unmatched_rows]
        end_row_un = len(un_values)
        end_col_letter = get_last_col_letter(n_cols)
        ws_un.update(range_name=f"A1:{end_col_letter}{end_row_un}",
                     values=un_values, value_input_option='USER_ENTERED')
        print(f"   {len(unmatched_rows)}行 書き込み完了", file=sys.stderr)

    print(f"\n✅ 完了", file=sys.stderr)
    print(f"バックアップ: {backup_name}")
    if unmatched_rows:
        print(f"未対応SKU: {INV_SHEET}_未対応SKU")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="書き込みせず結果のみ表示")
    p.add_argument("--apply", action="store_true", help="本実行")
    args = p.parse_args()

    if not (args.dry_run or args.apply):
        p.error("--dry-run または --apply を指定してください")

    process(dry_run=args.dry_run and not args.apply)


if __name__ == "__main__":
    main()
