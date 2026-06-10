#!/usr/bin/env python3
"""
販売実績が予想から大きく乖離した商品を ChatWork に通知。

判定条件 (両方を満たすと異常):
  - 乖離率 |実績 - 予想| / 予想 ≥ THRESHOLD_RATE (default 0.5 = 50%)
  - 絶対差 |実績 - 予想| ≥ THRESHOLD_ABS (default 5 個)

対象:
  tab_blocks_config.py の全11タブ × 全ブロック
  対象日: --date (default 昨日)
  - amazon: 全体の販売予想 (sales_forecast_row 等) vs 全体の販売実績
  - ここでは Amazon と楽天それぞれ別々に判定（タブ構造に応じて）

使い方:
  python3 notify_sales_anomaly.py                # 昨日、本番送信
  python3 notify_sales_anomaly.py --dry-run      # 通知内容を表示するだけ
  python3 notify_sales_anomaly.py --date 2026-06-09
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from tab_blocks_config import TAB_BLOCKS, get_blocks

DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)

THRESHOLD_RATE = 0.50  # 50%
THRESHOLD_ABS = 5      # 5 個


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def find_date_column(ws, date_str: str) -> int | None:
    target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    target_serial = (target - SERIAL_DATE_BASE).days
    row1_raw = ws.get('A1:ZZ1', value_render_option='UNFORMATTED_VALUE')
    row1 = row1_raw[0] if row1_raw else []
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and int(v) == target_serial:
            return i
    return None


def to_number(v) -> float | None:
    if v in (None, '', '#N/A'):
        return None
    try:
        return float(str(v).replace(',', ''))
    except (ValueError, TypeError):
        return None


def is_anomaly(forecast: float, actual: float) -> bool:
    if forecast <= 0:
        # 予想が 0/負 のケースは判定不能（実績が大きくても係数計算できない）
        return False
    diff_abs = abs(actual - forecast)
    if diff_abs < THRESHOLD_ABS:
        return False
    rate = diff_abs / forecast
    return rate >= THRESHOLD_RATE


def scan_tab(ws, tab_name: str, blocks: list, date_str: str) -> List[Tuple]:
    """各ブロックで Amazon/楽天 の異常を抽出。
    返り値: [(tab, code, channel, forecast, actual, rate), ...]
    """
    date_col = find_date_column(ws, date_str)
    if date_col is None:
        print(f"  [{tab_name}] 日付 {date_str} 列が見つからず → スキップ", file=sys.stderr)
        return []

    col = col_letter(date_col)

    # 必要な行を全部取得 (1行ずつ batch_get)
    rows_needed = set()
    for b in blocks:
        rows_needed.add(b["sales_forecast_row"])  # amazon販売予想
        rows_needed.add(b["sales_row"])           # amazonFBA販売実績
        if "rakuten_sales_forecast_row" in b:
            rows_needed.add(b["rakuten_sales_forecast_row"])
            rows_needed.add(b["rakuten_sales_row"])

    ranges = [f"{col}{r}:{col}{r}" for r in sorted(rows_needed)]
    results = ws.batch_get(ranges, value_render_option='UNFORMATTED_VALUE')
    row_to_val: dict[int, float | None] = {}
    for r, res in zip(sorted(rows_needed), results):
        v = None
        if res and res[0] and len(res[0]) > 0:
            v = to_number(res[0][0])
        row_to_val[r] = v

    anomalies = []
    for b in blocks:
        # Amazon
        f_a = row_to_val.get(b["sales_forecast_row"])
        a_a = row_to_val.get(b["sales_row"])
        if f_a is not None and a_a is not None and is_anomaly(f_a, a_a):
            rate = (a_a - f_a) / f_a if f_a > 0 else 0
            anomalies.append((tab_name, b["code"], "Amazon", f_a, a_a, rate))
        # 楽天
        if "rakuten_sales_row" in b:
            f_r = row_to_val.get(b["rakuten_sales_forecast_row"])
            a_r = row_to_val.get(b["rakuten_sales_row"])
            if f_r is not None and a_r is not None and is_anomaly(f_r, a_r):
                rate = (a_r - f_r) / f_r if f_r > 0 else 0
                anomalies.append((tab_name, b["code"], "楽天", f_r, a_r, rate))

    return anomalies


def post_to_chatwork(token: str, room_id: str, body: str) -> dict:
    url = f"https://api.chatwork.com/v2/rooms/{room_id}/messages"
    data = urllib.parse.urlencode({"body": body, "self_unread": "1"}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "X-ChatWorkToken": token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    parser.add_argument("--date", default=yesterday, help="判定日 (default: 昨日)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rate", type=float, default=THRESHOLD_RATE, help="乖離率閾値 (default 0.5)")
    parser.add_argument("--abs-diff", type=int, default=THRESHOLD_ABS,
                        help="絶対差閾値 (default 5)")
    args = parser.parse_args()

    global THRESHOLD_RATE, THRESHOLD_ABS
    THRESHOLD_RATE = args.rate
    THRESHOLD_ABS = args.abs_diff

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        if not settings.chatwork_api_token or not settings.chatwork_room_id:
            print("エラー: ChatWork 設定がありません (CHATWORK_API_TOKEN / CHATWORK_ROOM_ID)",
                  file=sys.stderr)
            sys.exit(1)

    import gspread
    from google.oauth2.service_account import Credentials
    import time

    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    sp = gc.open_by_key(DEST_SPREADSHEET_ID)

    print(f"=== 販売異常検知 [{args.date}] 閾値: ±{int(THRESHOLD_RATE*100)}% かつ ±{THRESHOLD_ABS}個 ===",
          file=sys.stderr)

    all_anomalies: List[Tuple] = []
    for tab_name, blocks in TAB_BLOCKS.items():
        try:
            ws = sp.worksheet(tab_name)
        except Exception as e:
            print(f"⚠ [{tab_name}] 取得失敗: {e}", file=sys.stderr)
            continue
        anomalies = scan_tab(ws, tab_name, blocks, args.date)
        print(f"  [{tab_name}] {len(anomalies)} 件の異常", file=sys.stderr)
        all_anomalies.extend(anomalies)
        time.sleep(2)

    print(f"\n=== 合計 {len(all_anomalies)} 件の異常 ===", file=sys.stderr)
    for t, code, ch, f, a, rate in all_anomalies:
        sign = "+" if rate > 0 else ""
        print(f"  {code} ({ch}) {t}: 予想={f:.1f} 実績={a:.0f} 差={a-f:+.0f} ({sign}{rate*100:.0f}%)",
              file=sys.stderr)

    if not all_anomalies:
        print("\n異常なし → 通知スキップ", file=sys.stderr)
        return

    # ChatWork 本文組み立て
    body_lines = [f"[info][title]🚨 販売異常検知 ({args.date})[/title]"]
    body_lines.append(f"判定: |実績-予想|/予想 ≥ {int(THRESHOLD_RATE*100)}% かつ |差| ≥ {THRESHOLD_ABS}個")
    body_lines.append("")
    # チャネル別にグループ化
    by_channel = {}
    for t, code, ch, f, a, rate in all_anomalies:
        by_channel.setdefault(ch, []).append((t, code, f, a, rate))
    for ch in ["Amazon", "楽天"]:
        items = by_channel.get(ch, [])
        if not items:
            continue
        body_lines.append(f"━━ {ch} ({len(items)}件) ━━")
        for t, code, f, a, rate in sorted(items, key=lambda x: -abs(x[4])):
            sign = "+" if rate > 0 else ""
            arrow = "📈" if rate > 0 else "📉"
            body_lines.append(f"{arrow} {code}: 予想 {f:.0f} → 実績 {a:.0f} ({sign}{rate*100:.0f}%)")
        body_lines.append("")
    body_lines.append("[/info]")
    body = "\n".join(body_lines)

    if args.dry_run:
        print("\n=== [dry-run] ChatWork に送信予定 ===", file=sys.stderr)
        print(body)
        return

    try:
        result = post_to_chatwork(
            settings.chatwork_api_token,
            settings.chatwork_room_id,
            body,
        )
        print(f"\n✓ ChatWork 送信完了 (message_id={result.get('message_id')})", file=sys.stderr)
    except Exception as e:
        print(f"\n✗ ChatWork 送信失敗: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
