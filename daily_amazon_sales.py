#!/usr/bin/env python3
"""
Amazon 3アカウントの日次販売実績を「縦=(ASIN,アカウント)、横=日付」で
Googleスプレッドシートに記録する。

SP-API の orderMetrics は ASIN 単位でしか販売数を返さないため、
SKU バリエーション (XXX(A), XXX(A-2)) ごとの内訳は取得できない。

シート構成（同一スプレッドシート内）:
  シート名: 「日次Amazon販売推移」
  - A列: ASIN
  - B列: アカウント名 (coconem / kk-trading / bulqrea)
  - 1行目: A1="ASIN", B1="アカウント", C1以降=日付
  - C列以降: 各日付の販売数（unitCount）

動作:
  - 既存の(ASIN,アカウント)ペアを読み、新規ペアが現れたら下に行を自動追加
  - 既存日列があれば上書き（再取得対応）
  - 新しい日付なら最終列の次に列追加

使い方:
  python3 daily_amazon_sales.py                                      # 昨日と一昨日
  python3 daily_amazon_sales.py --from-date 2026-06-01 --to-date 2026-06-02
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Dict, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.inventory import fetch_inventory
from src.sp_client import SPClient

SHEET_NAME = "日次Amazon販売推移"

# key = (asin, account_name)
SalesKey = Tuple[str, str]


def collect_asins_from_inventory(settings: Settings) -> Set[str]:
    """各アカウントの FBA 在庫から ASIN 一覧を取得して合算。"""
    all_asins: Set[str] = set()
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
            for item in items:
                if item.asin:
                    all_asins.add(item.asin)
            print(f"[Amazon:{acc.name}] {len(items)}件取得 → 累計ASIN={len(all_asins)}", file=sys.stderr)
        except Exception as e:
            print(f"[Amazon:{acc.name}] ASIN収集エラー: {e}", file=sys.stderr)
    return all_asins


def fetch_sales(
    settings: Settings,
    asins: Set[str],
    from_date: str,
    to_date: str,
) -> Dict[Tuple[str, str, str], int]:
    """
    返り値: {(asin, account_name, date_str): qty, ...}
    """
    result: Dict[Tuple[str, str, str], int] = {}
    interval_start = f"{from_date}T00:00:00+09:00"
    interval_end = f"{to_date}T23:59:59+09:00"

    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
        except Exception as e:
            print(f"[Amazon:{acc.name}] クライアント生成失敗: {e}", file=sys.stderr)
            continue

        for asin in sorted(asins):
            try:
                metrics = client.get_order_metrics(
                    interval_start=interval_start,
                    interval_end=interval_end,
                    granularity="Day",
                    asin=asin,
                )
            except Exception as e:
                # ASIN ごとのエラーは無視（その ASIN がそのアカウントに無い等）
                continue

            for m in metrics:
                interval = m.get("interval", "")
                # 例: "2026-06-01T00:00+09:00--2026-06-02T00:00+09:00"
                if "--" in interval:
                    start_part = interval.split("--")[0]
                    start_date = start_part.split("T")[0]
                else:
                    continue
                units = int(m.get("unitCount", 0) or 0)
                if units > 0:
                    result[(asin, acc.name, start_date)] = units
        print(f"[Amazon:{acc.name}] orderMetrics 取得完了", file=sys.stderr)

    return result


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def ensure_sheet(spreadsheet, row_count: int):
    import gspread
    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        rows = max(1 + row_count, 500)
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=rows, cols=400)
        ws.update(range_name="A1:B1", values=[["ASIN", "アカウント"]])
        ws.freeze(rows=1, cols=2)
        print(f"シート「{SHEET_NAME}」を新規作成しました", file=sys.stderr)
    return ws


def read_existing_keys(ws) -> List[SalesKey]:
    col_a = ws.col_values(1)
    col_b = ws.col_values(2)
    keys: List[SalesKey] = []
    n = max(len(col_a), len(col_b))
    for i in range(1, n):
        a = col_a[i] if i < len(col_a) else ""
        b = col_b[i] if i < len(col_b) else ""
        if a and b:
            keys.append((a, b))
    return keys


def append_new_key_rows(ws, new_keys: List[SalesKey], existing_count: int):
    if not new_keys:
        return
    start_row = 1 + existing_count + 1
    block = [[a, b] for (a, b) in new_keys]
    end_row = start_row + len(block) - 1
    if end_row > ws.row_count:
        ws.add_rows(end_row - ws.row_count + 100)
    rng = f"A{start_row}:B{end_row}"
    ws.update(range_name=rng, values=block)
    print(f"新規(ASIN,アカウント)ペア {len(new_keys)}件 を追加（{start_row}〜{end_row}行）", file=sys.stderr)


def get_date_columns(ws) -> Dict[str, int]:
    """1行目を読み、日付文字列 → 列番号 のマップを返す。"""
    row1 = ws.row_values(1)
    result: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if i < 3:
            continue
        if v:
            result[v] = i
    return result


def write_sales(
    spreadsheet,
    sales: Dict[Tuple[str, str, str], int],
    dates: List[str],
):
    ws = ensure_sheet(spreadsheet, len({(a, b) for (a, b, _d) in sales}))

    # 既存(ASIN, アカウント)ペアを取得
    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    # 必要な新規キーを抽出（販売数 > 0 のペアのみシートに記録する方針もあるが、
    # ここでは「対象ASIN×アカウント」を全部記録するため、保有ASIN一覧から作る）
    new_keys = [(a, b) for (a, b, _d) in sales if (a, b) not in existing_set]
    # 重複除去（順序保持）
    seen = set()
    new_keys_unique: List[SalesKey] = []
    for k in new_keys:
        if k in seen:
            continue
        seen.add(k)
        new_keys_unique.append(k)

    if new_keys_unique:
        append_new_key_rows(ws, new_keys_unique, len(existing_keys))
        existing_keys = existing_keys + new_keys_unique

    # 日付列を整える
    date_cols = get_date_columns(ws)
    # 新規日付を末尾に追加
    row1 = ws.row_values(1)
    next_col = max(len(row1) + 1, 3)
    new_date_cols_updates = []
    for d in dates:
        if d not in date_cols:
            date_cols[d] = next_col
            new_date_cols_updates.append({"range": f"{col_letter(next_col)}1", "values": [[d]]})
            next_col += 1
    if new_date_cols_updates:
        # 列数拡張
        max_col_needed = max(int(u["range"][:-1][0].rstrip(string_digits := "0123456789")) if False else next_col - 1, ws.col_count)
        if next_col - 1 > ws.col_count:
            ws.add_cols((next_col - 1) - ws.col_count + 10)
        ws.batch_update(new_date_cols_updates, value_input_option='USER_ENTERED')
        print(f"新規日付列 {len(new_date_cols_updates)}件 を追加", file=sys.stderr)

    # 値を書き込み（既存行→上書き、新規行→新規値）
    # 各 (ASIN, アカウント) の各日付セル
    # キーから行番号へマッピング
    key_to_row: Dict[SalesKey, int] = {}
    for idx, k in enumerate(existing_keys, start=2):  # ヘッダー1行目の次
        key_to_row[k] = idx

    updates = []
    for (a, b, d), qty in sales.items():
        if d not in date_cols:
            continue
        row = key_to_row.get((a, b))
        if row is None:
            continue
        col = col_letter(date_cols[d])
        updates.append({"range": f"{col}{row}", "values": [[qty]]})

    # 該当日列のうち販売がなかったセルは0で埋める（既存値が残らないように）
    # ただし「再取得」フローでは「販売なし=0」が正しい。
    zero_updates = []
    for k in existing_keys:
        for d in dates:
            if (k[0], k[1], d) in sales:
                continue
            if d not in date_cols:
                continue
            col = col_letter(date_cols[d])
            row = key_to_row[k]
            zero_updates.append({"range": f"{col}{row}", "values": [[0]]})

    all_updates = updates + zero_updates
    if all_updates:
        # バッチで書き込み
        BATCH = 200
        for i in range(0, len(all_updates), BATCH):
            chunk = all_updates[i:i + BATCH]
            ws.batch_update(chunk, value_input_option='USER_ENTERED')
        print(f"→ {len(updates)}件の販売数 + {len(zero_updates)}件の0埋め = 計{len(all_updates)}セル書き込み", file=sys.stderr)


def daterange(start_date: datetime.date, end_date: datetime.date) -> List[str]:
    """start ～ end（両端含む）の YYYY-MM-DD リスト。"""
    out = []
    d = start_date
    while d <= end_date:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def main():
    parser = argparse.ArgumentParser()
    today = datetime.date.today()
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=2)).strftime("%Y-%m-%d"),
                        help="開始日 (YYYY-MM-DD)。デフォルト: 2日前")
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
                        help="終了日 (YYYY-MM-DD)。デフォルト: 昨日")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次Amazon販売推移 [{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)

    # 1. ASIN 一覧
    asins = collect_asins_from_inventory(settings)
    print(f"\n対象 ASIN 数: {len(asins)}", file=sys.stderr)

    # 2. 販売実績を取得
    sales = fetch_sales(settings, asins, args.from_date, args.to_date)
    print(f"取得した販売レコード: {len(sales)}件", file=sys.stderr)

    # 3. シートに書き込み
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
    sp = gc.open_by_key(settings.google_spreadsheet_id)

    dates = daterange(
        datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date(),
        datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date(),
    )
    write_sales(sp, sales, dates)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
