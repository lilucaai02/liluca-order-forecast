#!/usr/bin/env python3
"""
全商品タブの過去イベント実績を「係数計算」タブのG列以降に書き出す。

各ブロック (タブ名+商品コード) について:
  - R6 周辺 (amazonイベント) に名前があり、かつ過去日であり、
  - R8 (Amazon平日平均販売点数) と R10 (amazonFBA販売実績) に有効値がある場合
  - 個別係数 = 実績 / 平均 を計算
  - イベント名は正規化（（仮）等を除去）

書き込み先「係数計算」タブのレイアウト:
  G1: 商品コード
  H1: イベント名（正規化後）
  I1: 日付
  J1: 平均販売数
  K1: 実績
  L1: 個別係数
  M1: 同商品×同イベントの平均係数
  N1: 同商品×同イベントの日数

各ブロックの R6 (event), R7 (coef), R8 (avg), R10 (actual) の行番号は
tab_blocks_config の sales_forecast_row(R9相当) を基準にして以下で求める:
  event_row = sales_forecast_row - 3
  coef_row  = sales_forecast_row - 2
  avg_row   = sales_forecast_row - 1
  actual_row = sales_forecast_row + 1

使い方:
  python3 dump_event_coefficients.py --dry-run
  python3 dump_event_coefficients.py
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
from tab_blocks_config import TAB_BLOCKS

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
TARGET_SHEET_NAME = "係数計算"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def normalize_event(name: str) -> str:
    s = name.strip()
    s = re.sub(r'[（(][^）)]*[）)]', '', s)
    s = re.sub(r'[:：]\s*パート\s*\d+', '', s)
    return s.strip()


def collect_from_tab(ws, blocks, today_serial):
    """1タブの全ブロックから (商品コード, イベント, 日付, avg, actual, coef) を取得。

    集計対象: R7 (amazonイベント係数) に値が入っている日。
    イベント名は R6 から取得。R6 が空欄でも、係数(R7)が連続して入っている期間中は
    直近の R6 イベント名を継承する。
    """
    if not blocks:
        return []

    # 各ブロックの行: event=sales_forecast_row-3, coef=-2, avg=-1, actual=+1
    rows_needed = set([1])  # 1行目（日付）
    for b in blocks:
        sf = b["sales_forecast_row"]
        rows_needed.update([sf - 3, sf - 2, sf - 1, sf + 1])

    last_col = ws.col_count
    last_col_letter = col_letter(last_col)
    rmin, rmax = min(rows_needed), max(rows_needed)
    rng = f"A{rmin}:{last_col_letter}{rmax}"
    data = ws.get(rng, value_render_option='UNFORMATTED_VALUE')

    def row_at(r):
        idx = r - rmin
        if 0 <= idx < len(data):
            return data[idx]
        return []

    dates = row_at(1)

    records = []  # (商品コード, イベント正規化, 日付str, avg, actual, coef)
    for b in blocks:
        sf = b["sales_forecast_row"]
        events = row_at(sf - 3)
        coefs = row_at(sf - 2)
        avgs = row_at(sf - 1)
        actuals = row_at(sf + 1)
        code = b["code"]

        last_event_name = ""  # 直近のイベント名を継承
        for ci in range(2, len(dates)):  # C列以降
            date_v = dates[ci] if ci < len(dates) else None
            if not isinstance(date_v, (int, float)):
                continue

            event_v = events[ci] if ci < len(events) else None
            coef_v = coefs[ci] if ci < len(coefs) else None

            # R6 にイベント名があれば継承を更新
            if event_v and isinstance(event_v, str) and event_v.strip():
                last_event_name = normalize_event(event_v.strip())

            # R7 (係数) が空欄ならイベント期間外 → 継承もリセット
            if coef_v is None or coef_v == '' or coef_v == '✕':
                last_event_name = ""
                continue
            # R7 が数値0の場合もリセット
            try:
                if float(coef_v) == 0:
                    last_event_name = ""
                    continue
            except (ValueError, TypeError):
                last_event_name = ""
                continue

            # この時点で R7 に有効な係数あり、かつイベント期間中
            if not last_event_name:
                # R6 が一度も登場していない期間は集計不可
                continue
            if int(date_v) >= today_serial:
                continue  # 未来日はスキップ

            avg_v = avgs[ci] if ci < len(avgs) else None
            actual_v = actuals[ci] if ci < len(actuals) else None

            try:
                avg = float(avg_v) if avg_v not in (None, '', '#N/A') else 0
                actual = float(actual_v) if actual_v not in (None, '', '#N/A') else 0
            except (ValueError, TypeError):
                continue

            if avg <= 0 or actual <= 0:
                continue

            date_str = (SERIAL_DATE_BASE + datetime.timedelta(days=int(date_v))).strftime("%Y-%m-%d")
            real_coef = actual / avg
            records.append((code, last_event_name, date_str, avg, actual, real_coef))

    return records


def main():
    parser = argparse.ArgumentParser()
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

    today_serial = (datetime.date.today() - SERIAL_DATE_BASE).days

    all_records: List[Tuple] = []
    for tab_name, blocks in TAB_BLOCKS.items():
        try:
            ws = sp.worksheet(tab_name)
        except Exception as e:
            print(f"⚠ [{tab_name}] 取得失敗: {e}", file=sys.stderr)
            continue
        recs = collect_from_tab(ws, blocks, today_serial)
        all_records.extend(recs)
        print(f"✓ [{tab_name}] {len(recs)} 件取得", file=sys.stderr)
        import time
        time.sleep(2)

    # (商品コード, イベント) ごとに平均係数と日数を計算
    by_key: Dict[Tuple[str, str], List[float]] = {}
    for code, event, _, _, _, coef in all_records:
        by_key.setdefault((code, event), []).append(coef)
    avg_by_key = {k: (sum(v) / len(v), len(v)) for k, v in by_key.items()}

    # 「係数計算」タブに書き込み
    target_ws = sp.worksheet(TARGET_SHEET_NAME)

    # G1:N1 ヘッダー
    header = [["商品コード", "イベント名", "日付", "平均販売数",
               "実績", "個別係数", "平均係数", "サンプル日数"]]

    # ソート（商品コード → イベント名 → 日付）
    all_records.sort(key=lambda r: (r[0], r[1], r[2]))

    # 各行データ
    data_rows = []
    for code, event, date_str, avg, actual, coef in all_records:
        avg_coef, n_days = avg_by_key[(code, event)]
        data_rows.append([code, event, date_str, round(avg, 2),
                          round(actual, 2), round(coef, 3),
                          round(avg_coef, 3), n_days])

    print(f"\n=== 集計 ===", file=sys.stderr)
    print(f"全レコード: {len(all_records)} 行", file=sys.stderr)
    print(f"(商品,イベント) ペア: {len(by_key)} 件", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 先頭10行プレビュー:", file=sys.stderr)
        for row in data_rows[:10]:
            print(f"  {row}", file=sys.stderr)
        return

    # G1:N{1+len(data_rows)} に書き込み
    end_row = 1 + len(data_rows)
    # 行数確保
    if end_row > target_ws.row_count:
        target_ws.add_rows(end_row - target_ws.row_count + 100)
    # 列数確保（N列=14列）
    if 14 > target_ws.col_count:
        target_ws.add_cols(14 - target_ws.col_count + 1)

    # ヘッダー
    target_ws.update(range_name="G1:N1", values=header)
    # データ
    if data_rows:
        target_ws.update(range_name=f"G2:N{end_row}", values=data_rows)
    print(f"\n→ 「{TARGET_SHEET_NAME}」タブ G1:N{end_row} に書き込み完了",
          file=sys.stderr)

    url = f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
    print(f"完了 → {url}")


if __name__ == "__main__":
    main()
