#!/usr/bin/env python3
"""
商品タブの数式セルを「常に正数を返す」形に書き換える。

修正内容:
  1. 予想セル (FBA在庫予想/RSL在庫予想/販売予想等) を MAX(0, ...) で囲む
  2. 実績セル (SUMIFS 由来) を IFERROR(..., 0) で囲む
  3. 既に IFERROR / MAX(0, ...) が頭にある式はスキップ（冪等性）

対象タブの A列を読み、行ラベルから対象セルを動的に判定する。

使い方:
  python3 clamp_to_positive.py --tab "DS-01 (在庫) "                # 全列対象
  python3 clamp_to_positive.py --tab "DS-01 (在庫) " --from-date 2026-01-01
  python3 clamp_to_positive.py --tab "DS-01 (在庫) " --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)

# 行ラベルごとの対処タイプ
WRAP_MAX_LABELS = {  # MAX(0, ...) で囲む（負値ガード）
    "amazon販売予想", "楽天販売予想", "Yahoo販売予想",
    "全体の販売予想", "amazonFBA販売実績",  # 実績も負にならないように
    "FBA在庫予想", "RSL在庫予想", "Stock Crew在庫予想",
    "在庫総数予想", "在庫総数実績",
    "FBA在庫実績", "RSL在庫実績", "Stock Crew在庫実績",
    "荒瀬倉庫 amazon用在庫予定", "荒瀬倉庫 amazon用在庫実績",
    "荒瀬倉庫 amazon以外在庫予定", "荒瀬倉庫 amazon以外在庫実績",
    "アールステージ在庫予定", "アールステージ在庫実績",
    "事務所 amazon用在庫予定", "事務所 amazon用在庫実績",
    "事務所 amazon以外在庫予定", "事務所 amazon以外在庫実績",
    "イーウーパスポート　中国在庫予定", "イーウーパスポート　中国在庫実績",
    "就労支援所在庫予定", "移動中予定", "移動中実績",
    "発注中", "入庫実績",
    "全体の販売実績", "楽天販売実績", "Yahoo販売実績",
}


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def find_last_date_column(ws) -> int:
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    last = 0
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and v > 40000:
            last = i
    return last


def find_date_column(ws, date_str: str) -> int | None:
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    target_serial = (target - SERIAL_DATE_BASE).days
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) == target_serial:
            return i
    return None


def transform_formula(formula: str) -> tuple[str, str] | None:
    """
    既存の数式を変換。返り値: (新数式, 種類) or None（変更不要）。
    全数式を IFERROR(MAX(0, ...), 0) で囲んで、エラーも負値も 0 にクランプ。
    冪等性: 既に IFERROR で囲まれているものはスキップ。
            既に MAX(0,...) だけのものは IFERROR を追加。
    """
    if not formula or not formula.startswith("="):
        return None

    body = formula[1:].strip()
    upper = body.upper()

    # 既に IFERROR で囲まれている → 対策済み、スキップ
    if upper.startswith("IFERROR("):
        return None

    # 既に MAX(0,...) で囲まれている → IFERROR を追加
    if upper.startswith("MAX(0,") or upper.startswith("MAX(0 ,"):
        new_body = f"IFERROR({body},0)"
        return f"={new_body}", "iferror_add"

    # それ以外（生の数式）→ IFERROR(MAX(0, ...), 0) で完全ラップ
    new_body = f"IFERROR(MAX(0,{body}),0)"
    return f"={new_body}", "both"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", required=True)
    parser.add_argument("--from-date",
                        help="開始日 (YYYY-MM-DD)。デフォルト: 全期間（C列〜）")
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
    sp = gc.open_by_key(DEST_SPREADSHEET_ID)
    ws = sp.worksheet(args.tab)

    col_a = ws.col_values(1)
    target_rows = []
    for r_idx, label in enumerate(col_a, start=1):
        if label.strip() in WRAP_MAX_LABELS:
            target_rows.append((r_idx, label.strip()))

    if not target_rows:
        print(f"⚠ {args.tab} に対象行ラベルがありません", file=sys.stderr)
        return

    last_col_idx = find_last_date_column(ws)
    if args.from_date:
        start_col_idx = find_date_column(ws, args.from_date)
        if start_col_idx is None:
            print(f"⚠ from-date {args.from_date} 列なし。C列から開始", file=sys.stderr)
            start_col_idx = 3
    else:
        start_col_idx = 3  # C列から

    print(f"=== [{args.tab}] 正数化書き換え ===", file=sys.stderr)
    print(f"対象行: {len(target_rows)} 行", file=sys.stderr)
    print(f"対象列: {col_letter(start_col_idx)} 〜 {col_letter(last_col_idx)} ({last_col_idx - start_col_idx + 1} 列)",
          file=sys.stderr)

    # 数式を1行ずつまとめて取得 → 書き換え判定
    start_col = col_letter(start_col_idx)
    last_col = col_letter(last_col_idx)

    updates = []
    sum_max_only = 0
    sum_both = 0
    skipped = 0

    # 全対象行を1回の batch_get でまとめて取得（API クォータ節約）
    ranges = [f"{start_col}{r_idx}:{last_col}{r_idx}" for r_idx, _ in target_rows]
    results = ws.batch_get(ranges, value_render_option='FORMULA')

    for (r_idx, label), row_data in zip(target_rows, results):
        if not row_data or not row_data[0]:
            continue
        cells = row_data[0]
        for c_off, val in enumerate(cells):
            col_idx = start_col_idx + c_off
            res = transform_formula(str(val) if val is not None else "")
            if res is None:
                skipped += 1
                continue
            new_formula, kind = res
            if kind == "max":
                sum_max_only += 1
            else:
                sum_both += 1
            updates.append({
                "range": f"{col_letter(col_idx)}{r_idx}",
                "values": [[new_formula]]
            })

    print(f"→ 書き換え対象: MAX のみ={sum_max_only} / IFERROR+MAX={sum_both} / "
          f"スキップ={skipped}", file=sys.stderr)
    print(f"合計セル: {len(updates)}", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 先頭3件:", file=sys.stderr)
        for u in updates[:3]:
            print(f"  {u['range']} ← {u['values'][0][0][:120]}...", file=sys.stderr)
        return

    BATCH = 200
    total = 0
    for i in range(0, len(updates), BATCH):
        chunk = updates[i:i + BATCH]
        ws.batch_update(chunk, value_input_option='USER_ENTERED')
        total += len(chunk)
        print(f"  {total}/{len(updates)} セル書き込み済み", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
