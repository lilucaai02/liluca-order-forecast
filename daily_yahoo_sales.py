#!/usr/bin/env python3
"""
Yahoo!ショッピング ストア管理API → 「日次Yahoo販売推移」シート

在庫にあるSKUを全て記録（取得に成功して販売0なら0埋め）。
SKUフォーマット: item_id または item_id:sub_code

シート構成:
  シート名: 「日次Yahoo販売推移」
  - A列: SKU (item_id もしくは item_id:sub_code)
  - B列: アカウント名 (Yahoo seller_id ベース)
  - 1行目: A1="SKU", B1="アカウント", C1以降=日付
  - C列以降: 各日付の販売数

動作 (2026-07-29 修正):
  - 取得に成功して販売0の日はセルに 0 が入る
  - 注文API取得に失敗したアカウントは「販売0」と区別し、該当セルを
    書き込まずに既存値を保持する (0埋めによる実績破壊の防止)
  - Yahoo は注文単位で取得するため、orderInfo を1件でも取りこぼすと
    どの SKU が欠けたか特定できない。よって失敗の単位はアカウント。

終了コード:
  0 = 全アカウント取得成功
  1 = 在庫由来SKUが0件、または全アカウント取得失敗 (書き込み中止)
  2 = 一部アカウントが取得失敗 (成功分のみ書き込み済み)

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
from typing import Dict, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.fetch_safety import (
    retry_call,
    set_default_socket_timeout,
    sheets_batch_update,
    sheets_retry,
)
from src.yahoo_client import YahooClient, YAHOO_QPS_SLEEP

SHEET_NAME = "日次Yahoo販売推移"
SalesKey = Tuple[str, str]  # (sku, account_name)


def _fmt_dt(d: datetime.date, is_end: bool = False) -> str:
    """orderList の期間フォーマット YYYYMMDDHH24MISS."""
    hhmmss = "235959" if is_end else "000000"
    return d.strftime("%Y%m%d") + hhmmss


def collect_yahoo_skus(settings: Settings) -> Tuple[Dict[SalesKey, List[str]], Set[str]]:
    """
    各Yahooアカウントの在庫全SKUを収集。
    返り値: ({(sku, account_name): [SKU候補文字列]}, 在庫取得に失敗したアカウント名)
    """
    all_map: Dict[SalesKey, List[str]] = {}
    failed_accounts: Set[str] = set()
    for acc in settings.get_yahoo_accounts():
        if not acc.refresh_token or not acc.client_secret:
            print(f"[Yahoo:{acc.name}] refresh_token / client_secret 未設定 → スキップ", file=sys.stderr)
            failed_accounts.add(acc.name)
            continue
        try:
            client = YahooClient(
                account_name=acc.name,
                client_id=acc.client_id,
                seller_id=acc.seller_id,
                client_secret=acc.client_secret,
                refresh_token=acc.refresh_token,
            )
            items = retry_call(lambda: client.get_store_items(),
                               f"[Yahoo:{acc.name}] itemSearch")
        except Exception as e:
            print(f"[Yahoo:{acc.name}] SKU収集エラー(このアカウントの実績は"
                  f"今回更新されません): {e}", file=sys.stderr)
            failed_accounts.add(acc.name)
            continue
        for item in items:
            sku = client.extract_sku(item)
            if not sku:
                continue
            key = (sku, acc.name)
            all_map[key] = [sku]
        print(f"[Yahoo:{acc.name}] 在庫から {len(items)} 商品確認", file=sys.stderr)
    return all_map, failed_accounts


def fetch_yahoo_sales(
    settings: Settings, from_date: str, to_date: str,
) -> Tuple[Dict[Tuple[str, str, str], int], Set[str]]:
    """
    orderList + orderInfo で期間内の (sku, account_name, YYYY-MM-DD) → 販売数 を返す。
    キャンセル(OrderStatus=4) は除外。

    返り値: (sales, failed_accounts)
    重要: failed_accounts のアカウントは「販売0」ではなく「不明」。
    書き込み側で0埋めしてはいけない (既存の正しい実績を壊すため)。
    """
    result: Dict[Tuple[str, str, str], int] = {}
    failed_accounts: Set[str] = set()
    from_dt = datetime.datetime.strptime(from_date, "%Y-%m-%d").date()
    to_dt = datetime.datetime.strptime(to_date, "%Y-%m-%d").date()
    from_s = _fmt_dt(from_dt, is_end=False)
    to_s = _fmt_dt(to_dt, is_end=True)

    for acc in settings.get_yahoo_accounts():
        if not acc.refresh_token or not acc.client_secret:
            failed_accounts.add(acc.name)
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
            order_ids = retry_call(
                lambda: client.search_orders(from_s, to_s, order_status=[1, 2, 3, 5]),
                f"[Yahoo:{acc.name}] orderList")
            print(f"[Yahoo:{acc.name}] 対象注文 {len(order_ids)}件", file=sys.stderr)
        except Exception as e:
            print(f"[Yahoo:{acc.name}] 注文検索失敗 "
                  f"(このアカウントは0埋めせず既存値を保持): {e}", file=sys.stderr)
            failed_accounts.add(acc.name)
            continue

        # orderInfo を1件でも取りこぼすと、どのSKUが欠けたか特定できない。
        # そのため注文詳細の失敗はアカウント単位の失敗として扱う。
        acc_result: Dict[Tuple[str, str, str], int] = {}
        account_failed = False
        for i, oid in enumerate(order_ids, start=1):
            try:
                detail = retry_call(lambda: client.get_order_detail(oid),
                                    f"[Yahoo:{acc.name}] orderInfo {oid}")
            except Exception as e:
                print(f"[Yahoo:{acc.name}] orderInfo {oid} 取得失敗 "
                      f"(このアカウントは0埋めせず既存値を保持): {e}", file=sys.stderr)
                account_failed = True
                break
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
                acc_result[key] = acc_result.get(key, 0) + qty
            if i % 20 == 0:
                print(f"  [Yahoo:{acc.name}] 詳細取得 {i}/{len(order_ids)}", file=sys.stderr)
            time.sleep(YAHOO_QPS_SLEEP)

        if account_failed:
            failed_accounts.add(acc.name)
            continue
        result.update(acc_result)

    return result, failed_accounts


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def ensure_sheet(spreadsheet, row_count: int):
    import gspread
    try:
        ws = sheets_retry(spreadsheet.worksheet, SHEET_NAME)
    except gspread.WorksheetNotFound:
        rows = max(1 + row_count, 500)
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=rows, cols=400)
        ws.update(range_name="A1:B1", values=[["SKU", "アカウント"]])
        ws.freeze(rows=1, cols=2)
        print(f"シート「{SHEET_NAME}」を新規作成", file=sys.stderr)
    return ws


def read_existing_keys(ws) -> List[SalesKey]:
    col_a = sheets_retry(ws.col_values, 1)
    col_b = sheets_retry(ws.col_values, 2)
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
        sheets_retry(ws.add_rows, end_row - ws.row_count + 100)
    sheets_retry(ws.update, range_name=f"A{start_row}:B{end_row}", values=block)
    print(f"新規(SKU,アカウント)ペア {len(new_keys)}件 を追加", file=sys.stderr)


def get_date_columns(ws) -> Dict[str, int]:
    row1 = sheets_retry(ws.row_values, 1)
    result: Dict[str, int] = {}
    for i, v in enumerate(row1, start=1):
        if i < 3:
            continue
        if v:
            result[v] = i
    return result


def write_sales(spreadsheet, sales: Dict[Tuple[str, str, str], int],
                dates: List[str], all_known: List[SalesKey],
                failed_accounts: Set[str] | None = None):
    """販売実績をシートへ書き込む。

    取得に失敗したアカウントのSKU、および今回問い合わせていないSKUの
    日付セルは書き込みをスキップし、既存値をそのまま残す。
    「取得成功して本当に販売0」のときだけ 0 を書く。
    """
    failed_accounts = failed_accounts or set()
    # 実際に取得を試みたキー:
    #   - 今回の在庫収集で得られた (SKU, アカウント)
    #   - 注文に現れた (SKU, アカウント) … 在庫一覧から消えていても実績は判明している
    attempted_keys: Set[SalesKey] = set(all_known)
    for (sku, acc, _d) in sales:
        attempted_keys.add((sku, acc))

    all_keys = sorted(attempted_keys)

    ws = ensure_sheet(spreadsheet, len(all_keys))
    existing_keys = read_existing_keys(ws)
    existing_set = set(existing_keys)
    new_keys = [k for k in all_keys if k not in existing_set]
    if new_keys:
        append_new_key_rows(ws, new_keys, len(existing_keys))
        existing_keys = existing_keys + new_keys

    date_cols = get_date_columns(ws)
    row1 = sheets_retry(ws.row_values, 1)
    next_col = max(len(row1) + 1, 3)
    new_date_updates = []
    for d in dates:
        if d not in date_cols:
            date_cols[d] = next_col
            new_date_updates.append({"range": f"{col_letter(next_col)}1", "values": [[d]]})
            next_col += 1
    if new_date_updates:
        if next_col - 1 > ws.col_count:
            sheets_retry(ws.add_cols, (next_col - 1) - ws.col_count + 10)
        sheets_batch_update(ws, new_date_updates, value_input_option='USER_ENTERED')

    key_to_row = {k: idx for idx, k in enumerate(existing_keys, start=2)}
    updates = []
    skipped_failed = 0
    skipped_unqueried = 0
    for key in existing_keys:
        row = key_to_row[key]
        if key[1] in failed_accounts:
            skipped_failed += 1
            continue
        if key not in attempted_keys:
            skipped_unqueried += 1
            continue
        for d in dates:
            if d not in date_cols:
                continue
            col = col_letter(date_cols[d])
            qty = sales.get((key[0], key[1], d), 0)
            updates.append({"range": f"{col}{row}", "values": [[qty]]})

    if skipped_failed:
        print(f"※ 取得失敗アカウント({', '.join(sorted(failed_accounts))})のため"
              f"書き込みをスキップ (既存値を保持): {skipped_failed}行", file=sys.stderr)
    if skipped_unqueried:
        print(f"※ 今回未問い合わせのため書き込みをスキップ: {skipped_unqueried}行",
              file=sys.stderr)

    BATCH = 200
    for i in range(0, len(updates), BATCH):
        sheets_batch_update(ws, updates[i:i+BATCH], value_input_option='USER_ENTERED')
    print(f"→ {len(updates)}セル書き込み（販売 {len(sales)}件 + 0埋め）", file=sys.stderr)


def daterange(start: datetime.date, end: datetime.date) -> List[str]:
    out = []
    d = start
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def main():
    set_default_socket_timeout()
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

    accounts = settings.get_yahoo_accounts()
    print(f"=== 日次Yahoo販売推移 [{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)
    sku_map, inv_failed = collect_yahoo_skus(settings)
    all_known = list(sku_map.keys())
    print(f"在庫由来のSKUペア: {len(all_known)}件", file=sys.stderr)

    if not all_known:
        print("エラー: 在庫から (SKU,アカウント) を1件も取得できませんでした。"
              "0埋めを防ぐため中止します", file=sys.stderr)
        sys.exit(1)

    sales, sales_failed = fetch_yahoo_sales(settings, args.from_date, args.to_date)
    print(f"取得した販売レコード: {len(sales)}件", file=sys.stderr)

    failed_accounts = inv_failed | sales_failed
    # 安全装置: 全アカウント取得失敗なら書き込まずに異常終了する
    if failed_accounts and len(failed_accounts) >= len(accounts):
        print(f"エラー: 全 {len(accounts)} Yahooアカウントで注文/在庫の取得に失敗しました "
              f"({', '.join(sorted(failed_accounts))})。"
              "0埋めを防ぐため書き込みを中止します", file=sys.stderr)
        sys.exit(1)
    if failed_accounts:
        print(f"警告: {len(failed_accounts)}アカウントの取得に失敗 "
              f"({', '.join(sorted(failed_accounts))}) "
              f"→ 該当セルは書き込まず既存値を保持します", file=sys.stderr)

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
    sp = sheets_retry(gc.open_by_key, settings.google_spreadsheet_id)

    dates = daterange(
        datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date(),
        datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date(),
    )
    write_sales(sp, sales, dates, all_known, failed_accounts=failed_accounts)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"\n完了 → {url}")
    if failed_accounts:
        # 転記側や cron から失敗を検知できるよう非ゼロで終了する
        sys.exit(2)


if __name__ == "__main__":
    main()
