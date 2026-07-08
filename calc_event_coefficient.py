#!/usr/bin/env python3
"""
amazonイベント係数 (R7) を過去の実績から逆算して予測。

ロジック:
  1. 全列スキャンして (日付, R6=イベント名, R8=平日平均, R10=販売実績) を取得
  2. 過去日 (今日以前) で R6 にイベント名が入っている列を抽出
  3. その列の「実際係数 = R10 / R8」を計算
  4. 同じイベント名でグループ化し、平均係数を算出
  5. 未来日 (明日以降) の R6 にイベント名がある列の R7 に予測係数を書き込み

例:
  過去の「ブラックフライデー」で R8=100, R10=300 → 係数=3.0
  同じ「ブラックフライデー」が未来日にあれば R7=3.0 を設定

注意:
  - R8 が 0/空欄、R10 が #N/A はスキップ
  - 既に R7 に値がある場合は上書き（dry-run で確認推奨）
  - amazonイベント (R6) が空欄なら係数も書かない

使い方:
  python3 calc_event_coefficient.py --tab "DS-01 (在庫) " --dry-run
  python3 calc_event_coefficient.py --tab "DS-01 (在庫) "
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def normalize_event_name(name: str) -> str:
    """イベント名を正規化: 括弧書き / ：パートX / 末尾「（仮）」等を除去。"""
    import re
    s = name.strip()
    # 全角・半角の括弧書きを除去（「（仮）」など）
    s = re.sub(r'[（(][^）)]*[）)]', '', s)
    # 「：パートX」「:パートX」を除去
    s = re.sub(r'[:：]\s*パート\s*\d+', '', s)
    return s.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--event-row", type=int, default=6, help="amazonイベント行 (default: 6)")
    parser.add_argument("--coef-row", type=int, default=7, help="amazonイベント係数行 (default: 7)")
    parser.add_argument("--avg-row", type=int, default=8, help="平日平均販売点数行 (default: 8)")
    parser.add_argument("--actual-row", type=int, default=10, help="amazonFBA販売実績行 (default: 10)")
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

    today_serial = (datetime.date.today() - SERIAL_DATE_BASE).days

    # 1行目（日付）と該当行（イベント/係数/平均/実績）を取得
    rows_needed = [1, args.event_row, args.coef_row, args.avg_row, args.actual_row]
    rmin, rmax = min(rows_needed), max(rows_needed)
    last_col = ws.col_count
    last_col_letter = col_letter(last_col)
    rng = f"A{rmin}:{last_col_letter}{rmax}"
    all_data = ws.get(rng, value_render_option='UNFORMATTED_VALUE')

    def row_at(r):
        idx = r - rmin
        if 0 <= idx < len(all_data):
            return all_data[idx]
        return []

    dates_row = row_at(1)
    event_row = row_at(args.event_row)
    coef_row = row_at(args.coef_row)
    avg_row = row_at(args.avg_row)
    actual_row = row_at(args.actual_row)

    # 各列の (日付シリアル, イベント名, 係数, 平均, 実績) を集計
    event_to_records: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    # 値 = (平均, 実績, 実際係数)
    future_event_cols: List[Tuple[int, str]] = []  # (列番号, イベント名)

    for col_idx in range(3, last_col + 1):  # C列以降
        c0 = col_idx - 1
        date_v = dates_row[c0] if c0 < len(dates_row) else None
        event_v = event_row[c0] if c0 < len(event_row) else None

        if not isinstance(date_v, (int, float)):
            continue
        if not event_v or not isinstance(event_v, str):
            continue

        event_name_raw = event_v.strip()
        event_name = normalize_event_name(event_name_raw)
        if not event_name:
            continue

        if int(date_v) < today_serial:
            # 過去日 → 実際係数を集計
            avg_v = avg_row[c0] if c0 < len(avg_row) else None
            actual_v = actual_row[c0] if c0 < len(actual_row) else None

            try:
                avg = float(avg_v) if avg_v not in (None, '', '#N/A') else 0
                actual = float(actual_v) if actual_v not in (None, '', '#N/A') else 0
            except (ValueError, TypeError):
                continue

            if avg <= 0 or actual <= 0:
                continue

            real_coef = actual / avg
            event_to_records[event_name].append((avg, actual, real_coef))
        else:
            # 未来日 → 係数書き込み対象
            future_event_cols.append((col_idx, event_name, event_name_raw))

    # 各イベントの平均係数
    event_to_avg_coef: Dict[str, Tuple[float, int]] = {}
    for event, records in event_to_records.items():
        coefs = [r[2] for r in records]
        avg_coef = sum(coefs) / len(coefs)
        event_to_avg_coef[event] = (avg_coef, len(records))

    print(f"=== [{args.tab}] amazonイベント係数 予測 ===", file=sys.stderr)
    print(f"\n== 過去日から検出したイベント別 平均係数 ==", file=sys.stderr)
    if not event_to_avg_coef:
        print("  (過去にイベント実績なし)", file=sys.stderr)
    for event, (coef, n) in sorted(event_to_avg_coef.items(), key=lambda x: -x[1][0]):
        print(f"  {event!r}: 平均係数 = {coef:.3f}  (n={n}日分)", file=sys.stderr)

    print(f"\n== 未来日のイベント列 (係数を書き込む対象) ==", file=sys.stderr)
    if not future_event_cols:
        print("  (未来日にイベント設定なし)", file=sys.stderr)

    updates = []
    skipped_unknown = []
    for col_idx, event, event_raw in future_event_cols:
        if event in event_to_avg_coef:
            coef = event_to_avg_coef[event][0]
            col = col_letter(col_idx)
            updates.append({
                "range": f"{col}{args.coef_row}",
                "values": [[round(coef, 3)]]
            })
            print(f"  {col}{args.coef_row} ({event_raw!r} → '{event}'): {coef:.3f}",
                  file=sys.stderr)
        else:
            skipped_unknown.append((col_idx, event, event_raw))

    if skipped_unknown:
        print(f"\n== スキップ (過去実績なしのイベント) ==", file=sys.stderr)
        for col_idx, event, event_raw in skipped_unknown:
            print(f"  {col_letter(col_idx)}{args.coef_row} ({event_raw!r} → '{event}')",
                  file=sys.stderr)

    print(f"\n書き込み対象: {len(updates)} セル", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き込みスキップ", file=sys.stderr)
        return

    if updates:
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            ws.batch_update(chunk, value_input_option='USER_ENTERED')
        print(f"→ {len(updates)} セル書き込み完了", file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
