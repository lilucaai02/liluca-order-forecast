#!/usr/bin/env python3
"""
楽天 RMS API → 「日次楽天販売推移」シート（縦=SKU×アカウント、横=日付）

楽天は SKU 単位で注文を取得できるため、Amazon と違い ASIN マッピングは不要。
SKU 形式: manageNumber:variantId （variantId が無ければ manageNumber のみ）

シート構成:
  シート名: 「日次楽天販売推移」
  - A列: SKU
  - B列: アカウント名
  - 1行目: A1="SKU", B1="アカウント", C1以降=日付
  - C列以降: 各日付の販売数

動作 (2026-07-29 修正):
  - 取得に成功して販売0の日はセルに 0 が入る
  - RMS API の取得に失敗したアカウントは「販売0」と区別し、該当セルを
    書き込まずに既存値を保持する (0埋めによる実績破壊の防止)
  - 楽天は SKU 単位ではなく注文単位で取得するため、注文の取りこぼしが
    あるとどの SKU が欠けたか特定できない。よって失敗の単位はアカウント。

終了コード:
  0 = 全アカウント取得成功
  1 = 在庫由来SKUが0件、または全アカウント取得失敗 (書き込み中止)
  2 = 一部アカウントが取得失敗 (成功分のみ書き込み済み)

使い方:
  python3 daily_rakuten_sales.py                                       # 昨日と一昨日
  python3 daily_rakuten_sales.py --from-date 2026-06-02 --to-date 2026-06-03
"""

from __future__ import annotations

import argparse
import base64
import datetime
import os
import sys
import time
from typing import Dict, List, Set, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.fetch_safety import (
    Pacer,
    RetryableStatus,
    retry_call,
    set_default_socket_timeout,
    sheets_batch_update,
    sheets_retry,
)
from src.inventory import fetch_rakuten_inventory
from src.rakuten_client import RakutenClient

SHEET_NAME = "日次楽天販売推移"
SEARCH_URL = "https://api.rms.rakuten.co.jp/es/2.0/order/searchOrder/"
GET_URL    = "https://api.rms.rakuten.co.jp/es/2.0/order/getOrder/"

# RMS 受注API は 1リクエスト/秒 程度が上限。余裕を見て 2.1 秒間隔
RMS_MIN_INTERVAL = 2.1
# 一時障害とみなす HTTP ステータス (429=スロットリング, 5xx=一時障害)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

SalesKey = Tuple[str, str]  # (sku, account_name)


def _post_with_retry(url: str, headers: dict, payload: dict, label: str,
                     pacer: Pacer, timeout: int = 60) -> dict:
    """RMS API へ POST。429/5xx/ネットワークエラーは指数バックオフでリトライ。

    リトライしても駄目なら例外を送出する。呼び出し側は必ず捕捉して
    「取得失敗」として記録すること (0埋めしてはいけない)。
    """
    def _call():
        pacer.wait()
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code in RETRYABLE_STATUS:
            raise RetryableStatus(
                f"status={resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            # 認証エラー等の恒久的失敗。リトライしても無駄なので即失敗
            raise RuntimeError(f"status={resp.status_code}: {resp.text[:200]}")
        return resp.json()

    return retry_call(_call, label)


def collect_rakuten_skus(settings: Settings) -> Tuple[List[SalesKey], Set[str]]:
    """各楽天アカウントの在庫から (SKU, account_name) ペア一覧を取得。

    返り値: (キー一覧, 在庫取得に失敗したアカウント名の集合)
    在庫取得に失敗したアカウントは SKU が1件も集まらないため、
    そのアカウントの実績は今回まったく更新されない (0埋めもしない)。
    """
    all_keys: List[SalesKey] = []
    failed_accounts: Set[str] = set()
    seen = set()
    for acc in settings.get_rakuten_accounts():
        try:
            client = RakutenClient(acc)
            items = retry_call(lambda: fetch_rakuten_inventory(client),
                               f"[楽天:{acc.name}] 在庫取得")
        except Exception as e:
            print(f"[楽天:{acc.name}] SKU収集エラー(このアカウントの実績は"
                  f"今回更新されません): {e}", file=sys.stderr)
            failed_accounts.add(acc.name)
            continue
        for item in items:
            if not item.seller_sku:
                continue
            key = (item.seller_sku, acc.name)
            if key in seen:
                continue
            seen.add(key)
            all_keys.append(key)
        print(f"[楽天:{acc.name}] 在庫から {len(items)} SKU 確認", file=sys.stderr)
    return all_keys, failed_accounts


def fetch_rakuten_sales(
    settings: Settings,
    from_date: str,
    to_date: str,
) -> Tuple[Dict[Tuple[str, str, str], int], Set[str]]:
    """期間内の販売数を取得する。

    返り値: (sales, failed_accounts)
      sales           = {(sku, account_name, date_str): qty, ...} 販売0は含めない
      failed_accounts = 注文取得に失敗したアカウント名の集合

    重要: failed_accounts のアカウントは「販売0」ではなく「不明」である。
    書き込み側で0埋めしてはいけない (既存の正しい実績を壊すため)。
    """
    result: Dict[Tuple[str, str, str], int] = {}
    failed_accounts: Set[str] = set()

    for acc in settings.get_rakuten_accounts():
        credential = f"{acc.service_secret}:{acc.license_key}"
        encoded = base64.b64encode(credential.encode()).decode()
        headers = {
            "Authorization": f"ESA {encoded}",
            "Content-Type": "application/json; charset=utf-8",
        }
        pacer = Pacer(RMS_MIN_INTERVAL)

        # --- searchOrder で注文番号一覧 -------------------------------------
        all_order_nums: List[str] = []
        page = 1
        account_failed = False
        while True:
            payload = {
                "dateType": 1,
                "startDatetime": f"{from_date}T00:00:00+0900",
                "endDatetime":   f"{to_date}T23:59:59+0900",
                "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page},
            }
            try:
                data = _post_with_retry(
                    SEARCH_URL, headers, payload,
                    f"[楽天:{acc.name}] searchOrder p{page}", pacer, timeout=30)
            except Exception as e:  # noqa: BLE001 - 失敗内容を残して継続
                print(f"[楽天:{acc.name}] searchOrder 取得失敗 "
                      f"(このアカウントは0埋めせず既存値を保持): {e}", file=sys.stderr)
                account_failed = True
                break
            nums = data.get("orderNumberList", []) or []
            if not nums:
                break
            all_order_nums.extend(nums)
            pag = data.get("PaginationResponseModel", {}) or {}
            if page >= pag.get("totalPages", 1):
                break
            page += 1

        if account_failed:
            failed_accounts.add(acc.name)
            continue

        all_order_nums = list(dict.fromkeys(all_order_nums))
        print(f"[楽天:{acc.name}] 注文番号 {len(all_order_nums)}件取得", file=sys.stderr)

        # --- getOrder で注文詳細 --------------------------------------------
        # 1バッチでも取りこぼすと、どのSKUが欠けたか特定できない。
        # そのためバッチ失敗はアカウント単位の失敗として扱う。
        acc_result: Dict[Tuple[str, str, str], int] = {}
        for i in range(0, len(all_order_nums), 100):
            batch = all_order_nums[i:i+100]
            payload = {"orderNumberList": batch, "version": 7}
            try:
                d = _post_with_retry(
                    GET_URL, headers, payload,
                    f"[楽天:{acc.name}] getOrder {i}-{i+len(batch)}", pacer, timeout=60)
            except Exception as e:  # noqa: BLE001 - 失敗内容を残して継続
                print(f"[楽天:{acc.name}] getOrder 取得失敗 "
                      f"(このアカウントは0埋めせず既存値を保持): {e}", file=sys.stderr)
                account_failed = True
                break
            for o in (d.get("OrderModelList") or []):
                odate_str = (o.get("orderDatetime") or "")[:10]
                if not odate_str:
                    continue
                for pkg in (o.get("PackageModelList") or []):
                    for item in (pkg.get("ItemModelList") or []):
                        mn = item.get("manageNumber", "")
                        vid = item.get("variantId", "") or ""
                        if not vid:
                            sku_list = item.get("SkuModelList") or []
                            if sku_list:
                                vid = (sku_list[0].get("variantId") or
                                       sku_list[0].get("merchantDefinedSkuId") or "")
                        sku = f"{mn}:{vid}" if vid else mn
                        qty = int(item.get("units", 0))
                        if not sku or qty <= 0:
                            continue
                        key = (sku, acc.name, odate_str)
                        acc_result[key] = acc_result.get(key, 0) + qty

        if account_failed:
            failed_accounts.add(acc.name)
            continue

        result.update(acc_result)
        print(f"[楽天:{acc.name}] getOrder 取得完了 "
              f"(販売レコード {len(acc_result)}件)", file=sys.stderr)

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
        print(f"シート「{SHEET_NAME}」を新規作成しました", file=sys.stderr)
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
    rng = f"A{start_row}:B{end_row}"
    sheets_retry(ws.update, range_name=rng, values=block)
    print(f"新規(SKU,アカウント)ペア {len(new_keys)}件 を追加（{start_row}〜{end_row}行）", file=sys.stderr)


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
                dates: List[str], all_known_keys: List[SalesKey] | None = None,
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
    attempted_keys: Set[SalesKey] = set(all_known_keys or [])
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
        print(f"新規日付列 {len(new_date_updates)}件 を追加", file=sys.stderr)

    key_to_row = {k: idx for idx, k in enumerate(existing_keys, start=2)}

    updates = []
    skipped_failed = 0
    skipped_unqueried = 0
    for key in existing_keys:
        row = key_to_row[key]
        if key[1] in failed_accounts:
            # 取得失敗アカウント。0埋めすると正しい実績を壊すのでスキップ
            skipped_failed += 1
            continue
        if key not in attempted_keys:
            # 在庫から消えた等で今回問い合わせていないSKU
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

    if updates:
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            sheets_batch_update(ws, chunk, value_input_option='USER_ENTERED')
        print(f"→ {len(updates)}セル書き込み（販売 {len(sales)}件 + 0埋め）", file=sys.stderr)


def daterange(start_date: datetime.date, end_date: datetime.date) -> List[str]:
    out = []
    d = start_date
    while d <= end_date:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def main():
    set_default_socket_timeout()
    parser = argparse.ArgumentParser()
    today = datetime.date.today()
    # 楽天 RMS もキャンセル等で確定が遅れるため、過去5日を再取得して上書き
    parser.add_argument("--from-date",
                        default=(today - datetime.timedelta(days=5)).strftime("%Y-%m-%d"))
    parser.add_argument("--to-date",
                        default=(today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

    if not settings.get_rakuten_accounts():
        print("エラー: 楽天アカウント未設定", file=sys.stderr)
        sys.exit(1)

    accounts = settings.get_rakuten_accounts()
    print(f"=== 日次楽天販売推移 [{args.from_date} ～ {args.to_date}] ===", file=sys.stderr)
    # 在庫から全 SKU を取得（販売 0 のものも含めて記録対象とするため）
    all_known_keys, inv_failed = collect_rakuten_skus(settings)
    print(f"在庫由来の (SKU,アカウント) ペア: {len(all_known_keys)} 件", file=sys.stderr)

    if not all_known_keys:
        print("エラー: 在庫から (SKU,アカウント) を1件も取得できませんでした。"
              "0埋めを防ぐため中止します", file=sys.stderr)
        sys.exit(1)

    sales, sales_failed = fetch_rakuten_sales(settings, args.from_date, args.to_date)
    print(f"取得した販売レコード(>0): {len(sales)}件", file=sys.stderr)

    failed_accounts = inv_failed | sales_failed
    # 安全装置: 全アカウントが取得失敗なら書き込まずに異常終了する
    if failed_accounts and len(failed_accounts) >= len(accounts):
        print(f"エラー: 全 {len(accounts)} アカウントで注文/在庫の取得に失敗しました "
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
    write_sales(sp, sales, dates, all_known_keys=all_known_keys,
                failed_accounts=failed_accounts)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"\n完了 → {url}")
    if failed_accounts:
        # 転記側や cron から失敗を検知できるよう非ゼロで終了する
        sys.exit(2)


if __name__ == "__main__":
    main()
