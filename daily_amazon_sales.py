#!/usr/bin/env python3
"""
Amazon 3アカウントの日次販売実績を「縦=(ASIN,アカウント)、横=日付」で
Googleスプレッドシートに記録する。

SP-API の orderMetrics は ASIN 単位でしか販売数を返さないため、
SKU バリエーション (XXX(A), XXX(A-2)) ごとの内訳は取得できない。
そのため C列に「対応SKU」（カンマ区切り）を併記する。

シート構成（同一スプレッドシート内）:
  シート名: 「日次Amazon販売推移」
  - A列: ASIN
  - B列: アカウント名 (coconem / kk-trading / bulqrea)
  - C列: 対応SKU (同じASIN×アカウントの全SKUをカンマ区切りで表記)
  - 1行目: A1="ASIN", B1="アカウント", C1="対応SKU", D1以降=日付
  - D列以降: 各日付の販売数（unitCount。ASIN×アカウント合計）

動作:
  - 在庫に存在する 全ASIN × そのASINを取り扱うアカウント の全ペアを記録
  - 販売 0 の日もセルに 0 が入る
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


def collect_asin_sku_map(settings: Settings) -> Dict[SalesKey, List[str]]:
    """
    各アカウントの FBA 在庫から (ASIN, アカウント) → [SKU リスト] を構築。
    """
    result: Dict[SalesKey, List[str]] = {}
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
            for item in items:
                if not item.asin or not item.seller_sku:
                    continue
                key = (item.asin, acc.name)
                result.setdefault(key, []).append(item.seller_sku)
            print(f"[Amazon:{acc.name}] {len(items)}件取得", file=sys.stderr)
        except Exception as e:
            print(f"[Amazon:{acc.name}] ASIN/SKU収集エラー: {e}", file=sys.stderr)
    return result


def fetch_sales(
    settings: Settings,
    asin_keys: List[SalesKey],
    from_date: str,
    to_date: str,
) -> Dict[Tuple[str, str, str], int]:
    """
    返り値: {(asin, account_name, date_str): qty, ...}
    販売 0 は含めない。書き込み側で0埋めする。
    """
    result: Dict[Tuple[str, str, str], int] = {}
    interval_start = f"{from_date}T00:00:00+09:00"
    interval_end = f"{to_date}T23:59:59+09:00"

    # アカウントごとにクライアントを使い回し
    asin_by_account: Dict[str, List[str]] = {}
    for (asin, acc_name) in asin_keys:
        asin_by_account.setdefault(acc_name, []).append(asin)

    for acc in settings.get_accounts():
        asins_for_acc = asin_by_account.get(acc.name, [])
        if not asins_for_acc:
            continue
        try:
            client = SPClient(settings, account=acc)
        except Exception as e:
            print(f"[Amazon:{acc.name}] クライアント生成失敗: {e}", file=sys.stderr)
            continue

        for asin in sorted(set(asins_for_acc)):
            try:
                metrics = client.get_order_metrics(
                    interval_start=interval_start,
                    interval_end=interval_end,
                    granularity="Day",
                    asin=asin,
                )
            except Exception:
                continue

            for m in metrics:
                interval = m.get("interval", "")
                if "--" in interval:
                    start_part = interval.split("--")[0]
                    start_date = start_part.split("T")[0]
                else:
                    continue
                units = int(m.get("unitCount", 0) or 0)
                if units > 0:
                    result[(asin, acc.name, start_date)] = units
        print(f"[Amazon:{acc.name}] orderMetrics 取得完了 ({len(set(asins_for_acc))} ASIN)",
              file=sys.stderr)

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
        ws.update(range_name="A1:C1", values=[["ASIN", "アカウント", "対応SKU"]])
        ws.freeze(rows=1, cols=3)
        print(f"シート「{SHEET_NAME}」を新規作成しました", file=sys.stderr)
    return ws


def read_existing_keys(ws) -> List[SalesKey]:
    """A列(ASIN), B列(アカウント)からペアを順序保持で取得。"""
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


def append_new_key_rows(ws, new_keys: List[SalesKey],
                        sku_map: Dict[SalesKey, List[str]],
                        existing_count: int):
    """新規(ASIN,アカウント)ペアを A,B,C列 (ASIN/アカウント/SKU) に追加。"""
    if not new_keys:
        return
    start_row = 1 + existing_count + 1
    block = []
    for (a, b) in new_keys:
        skus = ", ".join(sorted(set(sku_map.get((a, b), []))))
        block.append([a, b, skus])
    end_row = start_row + len(block) - 1
    if end_row > ws.row_count:
        ws.add_rows(end_row - ws.row_count + 100)
    rng = f"A{start_row}:C{end_row}"
    ws.update(range_name=rng, values=block)
    print(f"新規(ASIN,アカウント)ペア {len(new_keys)}件 を追加（{start_row}〜{end_row}行）",
          file=sys.stderr)


def update_sku_column(ws, all_keys: List[SalesKey],
                      sku_map: Dict[SalesKey, List[str]],
                      first_data_row: int = 2):
    """既存全ペアの C列(対応SKU) を最新の在庫情報で上書き。"""
    if not all_keys:
        return
    updates = []
    for idx, key in enumerate(all_keys, start=first_data_row):
        skus = ", ".join(sorted(set(sku_map.get(key, []))))
        updates.append({"range": f"C{idx}", "values": [[skus]]})
    if updates:
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            ws.batch_update(chunk, value_input_option='USER_ENTERED')


def get_date_columns(ws) -> Dict[str, int]:
    """1行目から日付文字列→列番号のマップを返す（D列以降）。"""
    row1 = ws.row_values(1)
    result: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if i < 4:  # A,B,C は固定列
            continue
        if v:
            result[v] = i
    return result


def write_sales(
    spreadsheet,
    sales: Dict[Tuple[str, str, str], int],
    sku_map: Dict[SalesKey, List[str]],
    dates: List[str],
):
    # すべての (ASIN, アカウント) ペアを書き込み対象とする（在庫保持中の全ペア）
    all_keys: List[SalesKey] = list(sku_map.keys())

    ws = ensure_sheet(spreadsheet, len(all_keys))

    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in all_keys if k not in existing_set]

    if new_keys:
        append_new_key_rows(ws, new_keys, sku_map, len(existing_keys))
        existing_keys = existing_keys + new_keys

    # SKU列を最新化（既存行も含めて在庫から再構築）
    update_sku_column(ws, existing_keys, sku_map)

    # 日付列
    date_cols = get_date_columns(ws)
    row1 = ws.row_values(1)
    next_col = max(len(row1) + 1, 4)
    new_date_updates = []
    for d in dates:
        if d not in date_cols:
            date_cols[d] = next_col
            new_date_updates.append({"range": f"{col_letter(next_col)}1", "values": [[d]]})
            next_col += 1
    if new_date_updates:
        if next_col - 1 > ws.col_count:
            ws.add_cols((next_col - 1) - ws.col_count + 10)
        ws.batch_update(new_date_updates, value_input_option='USER_ENTERED')
        print(f"新規日付列 {len(new_date_updates)}件 を追加", file=sys.stderr)

    # 行番号マッピング
    key_to_row: Dict[SalesKey, int] = {k: idx for idx, k in enumerate(existing_keys, start=2)}

    # 全ペア × 全日付セルを 0 or 販売数で埋める
    updates = []
    for key in existing_keys:
        row = key_to_row[key]
        for d in dates:
            if d not in date_cols:
                continue
            col = col_letter(date_cols[d])
            qty = sales.get((key[0], key[1], d), 0)
            updates.append({"range": f"{col}{row}", "values": [[qty]]})

    if updates:
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            ws.batch_update(chunk, value_input_option='USER_ENTERED')
        print(f"→ {len(updates)}セル書き込み（販売 {len(sales)}件 + 0埋め）",
              file=sys.stderr)


def daterange(start_date: datetime.date, end_date: datetime.date) -> List[str]:
    out = []
    d = start_date
    while d <= end_date:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def main():
    parser = argparse.ArgumentParser()
    today = datetime.date.today()
    # SP-API は当日のデータが翌朝までに集計しきれない場合があるため、
    # デフォルトで過去5日を再取得して確定値で上書きする
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=5)).strftime("%Y-%m-%d"))
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次Amazon販売推移 [{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)

    # 1. 在庫から (ASIN,アカウント) → [SKU] マップを構築
    sku_map = collect_asin_sku_map(settings)
    print(f"\n対象 (ASIN,アカウント) ペア数: {len(sku_map)}", file=sys.stderr)

    # 2. 販売実績を取得
    sales = fetch_sales(settings, list(sku_map.keys()), args.from_date, args.to_date)
    print(f"取得した販売レコード(>0): {len(sales)}件", file=sys.stderr)

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
    write_sales(sp, sales, sku_map, dates)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
