#!/usr/bin/env python3
"""
Yahoo!ショッピング ストア管理API → 「日次Yahoo販売推移」シート

在庫にあるSKUを全て記録（販売0も0埋め）。
SKUフォーマット: item_id または item_id:sub_code

シート構成:
  シート名: 「日次Yahoo販売推移」
  - A列: SKU (item_id もしくは item_id:sub_code)
  - B列: アカウント名 (Yahoo seller_id ベース)
  - 1行目: A1="SKU", B1="アカウント", C1以降=日付
  - C列以降: 各日付の販売数

使い方:
  python3 daily_yahoo_sales.py                                    # 昨日と5日前
  python3 daily_yahoo_sales.py --from-date 2026-07-15 --to-date 2026-07-21
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.yahoo_client import YahooClient, YAHOO_QPS_SLEEP

SHEET_NAME = "日次Yahoo販売推移"
SalesKey = Tuple[str, str]  # (sku, account_name)


def _fmt_dt(d: datetime.date, is_end: bool = False) -> str:
    """orderList の期間フォーマット YYYYMMDDHH24MISS."""
    hhmmss = "235959" if is_end else "000000"
    return d.strftime("%Y%m%d") + hhmmss


def collect_yahoo_skus(settings: Settings) -> Dict[SalesKey, List[str]]:
    """
    各Yahooアカウントの在庫全SKUを収集。
    返り値: {(sku, account_name): [SKU候補文字列]}
    """
    all_map: Dict[SalesKey, List[str]] = {}
    for acc in settings.get_yahoo_accounts():
        if not acc.refresh_token or not acc.client_secret:
            print(f"[Yahoo:{acc.name}] refresh_token / client_secret 未設定 → スキップ", file=sys.stderr)
            continue
        try:
            client = YahooClient(
                account_name=acc.name,
                client_id=acc.client_id,
                seller_id=acc.seller_id,
                client_secret=acc.client_secret,
                refresh_token=acc.refresh_token,
            )
            items = client.get_store_items()
        except Exception as e:
            print(f"[Yahoo:{acc.name}] SKU収集エラー: {e}", file=sys.stderr)
            continue
        for item in items:
            sku = client.extract_sku(item)
            if not sku:
                continue
            key = (sku, acc.name)
            all_map[key] = [sku]
        print(f"[Yahoo:{acc.name}] 在庫から {len(items)} 商品確認", file=sys.stderr)
    return all_map


def fetch_yahoo_sales(
    settings: Settings, from_date: str, to_date: str,
) -> Dict[Tuple[str, str, str], int]:
    """
    orderList + orderInfo で期間内の (sku, account_name, YYYY-MM-DD) → 販売数 を返す。
    キャンセル(OrderStatus=4) は除外。
    """
    result: Dict[Tuple[str, str, str], int] = {}
    ok_accounts = 0
    from_dt = datetime.datetime.strptime(from_date, "%Y-%m-%d").date()
    to_dt = datetime.datetime.strptime(to_date, "%Y-%m-%d").date()
    from_s = _fmt_dt(from_dt, is_end=False)
    to_s = _fmt_dt(to_dt, is_end=True)

    for acc in settings.get_yahoo_accounts():
        if not acc.refresh_token or not acc.client_secret:
            continue
        try:
            client = YahooClient(
                account_name=acc.name,
                client_id=acc.client_id,
                seller_id=acc.seller_id,
                client_secret=acc.client_secret,
                refresh_token=acc.refresh_token,
            )
            # 期間内の注文番号一覧（キャンセル除く: OrderStatus 1,2,3,5）
            order_ids = client.search_orders(from_s, to_s, order_status=[1, 2, 3, 5])
            print(f"[Yahoo:{acc.name}] 対象注文 {len(order_ids)}件", file=sys.stderr)
            ok_accounts += 1
        except Exception as e:
            print(f"[Yahoo:{acc.name}] 注文検索失敗: {e}", file=sys.stderr)
            continue

        for i, oid in enumerate(order_ids, start=1):
            try:
                detail = client.get_order_detail(oid)
            except Exception as e:
                print(f"[Yahoo:{acc.name}] orderInfo {oid} 失敗: {e}", file=sys.stderr)
                continue
            odt = detail.get("order_time", "")[:10]  # YYYY-MM-DD
            if not odt:
                continue
            for item in detail.get("items", []):
                sku = item["item_id"]
                if item.get("sub_code"):
                    sku = f"{sku}:{item['sub_code']}"
                qty = int(item.get("quantity", 0) or 0)
                if qty <= 0:
                    continue
                key = (sku, acc.name, odt)
                result[key] = result.get(key, 0) + qty
            if i % 20 == 0:
                print(f"  [Yahoo:{acc.name}] 詳細取得 {i}/{len(order_ids)}", file=sys.stderr)
            time.sleep(YAHOO_QPS_SLEEP)

    if ok_accounts == 0:
        # 全アカウント取得失敗 (注文API未承認の403など)。
        # 0埋めすると「売上0」と区別が付かなくなるため書き込みを中止する
        raise RuntimeError("全Yahooアカウントで注文取得に失敗 (0埋めを防ぐため中止)")

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
        ws.update(range_name="A1:B1", values=[["SKU", "アカウント"]])
        ws.freeze(rows=1, cols=2)
        print(f"シート「{SHEET_NAME}」を新規作成", file=sys.stderr)
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
    ws.update(range_name=f"A{start_row}:B{end_row}", values=block)
    print(f"新規(SKU,アカウント)ペア {len(new_keys)}件 を追加", file=sys.stderr)


def get_date_columns(ws) -> Dict[str, int]:
    row1 = ws.row_values(1)
    result: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if i < 3:
            continue
        if v:
            result[v] = i
    return result


def write_sales(spreadsheet, sales: Dict[Tuple[str, str, str], int],
                dates: List[str], all_known: List[SalesKey]):
    all_keys_set = set(all_known)
    for (sku, acc, _d) in sales:
        all_keys_set.add((sku, acc))
    all_keys = sorted(all_keys_set)

    ws = ensure_sheet(spreadsheet, len(all_keys))
    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in all_keys if k not in existing_set]
    if new_keys:
        append_new_key_rows(ws, new_keys, len(existing_keys))
        existing_keys = existing_keys + new_keys

    date_cols = get_date_columns(ws)
    row1 = ws.row_values(1)
    next_col = max(len(row1) + 1, 3)
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

    key_to_row = {k: idx for idx, k in enumerate(existing_keys, start=2)}
    updates = []
    for key in existing_keys:
        row = key_to_row[key]
        for d in dates:
            if d not in date_cols:
                continue
            col = col_letter(date_cols[d])
            qty = sales.get((key[0], key[1], d), 0)
            updates.append({"range": f"{col}{row}", "values": [[qty]]})

    BATCH = 200
    for i in range(0, len(updates), BATCH):
        ws.batch_update(updates[i:i+BATCH], value_input_option='USER_ENTERED')
    print(f"→ {len(updates)}セル書き込み（販売 {len(sales)}件 + 0埋め）", file=sys.stderr)


def daterange(start: datetime.date, end: datetime.date) -> List[str]:
    out = []
    d = start
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def main():
    parser = argparse.ArgumentParser()
    today = datetime.date.today()
    # 楽天と同じく過去5日再取得（集計遅延対策）
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=5)).strftime("%Y-%m-%d"))
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)
    if not settings.get_yahoo_accounts():
        print("エラー: Yahooアカウント未設定", file=sys.stderr)
        sys.exit(1)

    print(f"=== 日次Yahoo販売推移 [{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)
    all_known = list(collect_yahoo_skus(settings).keys())
    print(f"在庫由来のSKUペア: {len(all_known)}件", file=sys.stderr)

    sales = fetch_yahoo_sales(settings, args.from_date, args.to_date)
    print(f"取得した販売レコード: {len(sales)}件", file=sys.stderr)

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
    write_sales(sp, sales, dates, all_known)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"\n完了 → {url}")


if __name__ == "__main__":
    main()
