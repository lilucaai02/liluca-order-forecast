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
  - 取得に成功して販売 0 の日はセルに 0 が入る
  - SP-API の取得に失敗した (ASIN,アカウント) は「販売0」と区別し、
    該当セルを書き込まずに既存値を保持する（0埋めによる実績破壊の防止）
  - 既存日列があれば上書き（再取得対応）
  - 新しい日付なら最終列の次に列追加

終了コード:
  0 = 全ASIN取得成功
  1 = 在庫が0件、または全ASIN取得失敗（書き込み中止）
  2 = 一部ASINが取得失敗（成功分のみ書き込み済み）

使い方:
  python3 daily_amazon_sales.py                                      # 昨日と一昨日
  python3 daily_amazon_sales.py --from-date 2026-06-01 --to-date 2026-06-02
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
from src.inventory import fetch_inventory
from src.sp_client import SPClient

SHEET_NAME = "日次Amazon販売推移"

# key = (asin, account_name)
SalesKey = Tuple[str, str]

# --- SP-API getOrderMetrics のレート制限対策 --------------------------------
# getOrderMetrics の公称レートは 0.5 req/秒 (バースト分を使い切ると以降は
# QuotaExceeded)。ASIN 単位で 150 回以上叩くため、間隔を空けないと後半の
# ASIN が高確率で QuotaExceeded になる。
ORDER_METRICS_MIN_INTERVAL = 2.0  # 秒。呼び出し間隔の下限
ORDER_METRICS_MAX_ATTEMPTS = 5    # 初回 + リトライ4回
ORDER_METRICS_BACKOFF_START = 5.0
ORDER_METRICS_BACKOFF_MAX = 60.0

# リトライ対象と判断するエラー文字列 (スロットリング / 一時障害)
RETRYABLE_MARKERS = (
    "quotaexceeded",
    "429",
    "too many requests",
    "throttl",
    "500",
    "502",
    "503",
    "504",
    "internalerror",
    "internalfailure",
    "internal server error",
    "serviceunavailable",
    "service unavailable",
    "gateway",
    "timeout",
    "timed out",
    "connection",
    "temporarily",
)

# 最後に getOrderMetrics を呼んだ時刻 (ペーシング用)
_last_metrics_call: List[float] = [0.0]


def _is_retryable(exc: BaseException) -> bool:
    """スロットリング/一時障害なら True。権限エラー等の恒久的失敗は False。"""
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(marker in msg for marker in RETRYABLE_MARKERS)


def _pace_order_metrics() -> None:
    """前回呼び出しから ORDER_METRICS_MIN_INTERVAL 秒空ける。"""
    elapsed = time.monotonic() - _last_metrics_call[0]
    if _last_metrics_call[0] and elapsed < ORDER_METRICS_MIN_INTERVAL:
        time.sleep(ORDER_METRICS_MIN_INTERVAL - elapsed)
    _last_metrics_call[0] = time.monotonic()


def fetch_order_metrics_with_retry(client, asin: str, account_name: str,
                                   interval_start: str, interval_end: str) -> List[dict]:
    """getOrderMetrics を指数バックオフでリトライ。

    ORDER_METRICS_MAX_ATTEMPTS 回試しても駄目なら最後の例外を送出する。
    呼び出し側で必ず捕捉し「取得失敗」として記録すること
    (取得失敗を販売0として書き込んではいけない)。
    """
    delay = ORDER_METRICS_BACKOFF_START
    last_exc: BaseException | None = None
    for attempt in range(1, ORDER_METRICS_MAX_ATTEMPTS + 1):
        _pace_order_metrics()
        try:
            return client.get_order_metrics(
                interval_start=interval_start,
                interval_end=interval_end,
                granularity="Day",
                asin=asin,
            )
        except Exception as e:  # noqa: BLE001 - 種別は _is_retryable で判定
            last_exc = e
            if attempt >= ORDER_METRICS_MAX_ATTEMPTS or not _is_retryable(e):
                raise
            print(f"  [Amazon:{account_name}] ASIN {asin} 一時エラー "
                  f"({attempt}/{ORDER_METRICS_MAX_ATTEMPTS - 1}) "
                  f"{delay:.0f}秒待機: {e}", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, ORDER_METRICS_BACKOFF_MAX)
    assert last_exc is not None
    raise last_exc


def fetch_inventory_with_retry(client, account_name: str):
    """FBA在庫取得を指数バックオフでリトライ。

    在庫取得が失敗するとそのアカウントの全ASINが販売取得の対象から外れ、
    実績が丸ごと欠測するため、orderMetrics と同様にリトライする。
    """
    delay = ORDER_METRICS_BACKOFF_START
    for attempt in range(1, ORDER_METRICS_MAX_ATTEMPTS + 1):
        try:
            return fetch_inventory(client)
        except Exception as e:  # noqa: BLE001 - 種別は _is_retryable で判定
            if attempt >= ORDER_METRICS_MAX_ATTEMPTS or not _is_retryable(e):
                raise
            print(f"  [Amazon:{account_name}] 在庫取得 一時エラー "
                  f"({attempt}/{ORDER_METRICS_MAX_ATTEMPTS - 1}) "
                  f"{delay:.0f}秒待機: {e}", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, ORDER_METRICS_BACKOFF_MAX)


def with_retry(fn, *args, **kwargs):
    """Sheets API の 429 (quota超過) を指数バックオフでリトライ。

    注意: gspread の batch_update は渡した data の range をシート名付きに
    書き換える（破壊的変更）ため、リトライで同じリストを再利用してはいけない。
    batch_update をリトライする場合は retry_batch_update() を使うこと。
    """
    import time
    from gspread.exceptions import APIError
    delay = 30
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            if "429" in str(e) and attempt < 5:
                print(f"  [quota超過] {delay}秒待機してリトライ ({attempt + 1}/5)",
                      file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                raise


def retry_batch_update(ws, chunk, **kwargs):
    """batch_update 専用リトライ。毎試行で chunk のコピーを渡す。"""
    return with_retry(lambda: ws.batch_update([dict(u) for u in chunk], **kwargs))


def collect_asin_sku_map(settings: Settings) -> Dict[SalesKey, List[str]]:
    """
    各アカウントの FBA 在庫から (ASIN, アカウント) → [SKU リスト] を構築。
    """
    result: Dict[SalesKey, List[str]] = {}
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory_with_retry(client, acc.name)
            for item in items:
                if not item.asin or not item.seller_sku:
                    continue
                key = (item.asin, acc.name)
                result.setdefault(key, []).append(item.seller_sku)
            print(f"[Amazon:{acc.name}] {len(items)}件取得", file=sys.stderr)
        except Exception as e:
            # 在庫取得に失敗するとこのアカウントのASINが1件も対象にならず、
            # 販売実績の書き込み側でも「未問い合わせ」としてスキップされる。
            # 実績は壊れないが欠測になるため、はっきり警告を出す。
            print(f"[Amazon:{acc.name}] ASIN/SKU収集エラー(このアカウントの実績は"
                  f"今回更新されません): {e}", file=sys.stderr)
    return result


def fetch_sales(
    settings: Settings,
    asin_keys: List[SalesKey],
    from_date: str,
    to_date: str,
) -> Tuple[Dict[Tuple[str, str, str], int], Set[SalesKey]]:
    """
    返り値: (sales, failed_keys)
      sales       = {(asin, account_name, date_str): qty, ...} 販売 0 は含めない
      failed_keys = 取得に失敗した (asin, account_name) の集合

    重要: failed_keys のペアは「販売0」ではなく「不明」である。
    書き込み側で 0 埋めしてはいけない (既存の正しい実績を壊すため)。
    """
    result: Dict[Tuple[str, str, str], int] = {}
    failed_keys: Set[SalesKey] = set()
    interval_start = f"{from_date}T00:00:00+09:00"
    interval_end = f"{to_date}T23:59:59+09:00"

    # アカウントごとにクライアントを使い回し
    asin_by_account: Dict[str, List[str]] = {}
    for (asin, acc_name) in asin_keys:
        asin_by_account.setdefault(acc_name, []).append(asin)

    for acc in settings.get_accounts():
        asins_for_acc = sorted(set(asin_by_account.get(acc.name, [])))
        if not asins_for_acc:
            continue
        try:
            client = SPClient(settings, account=acc)
        except Exception as e:
            # クライアントが作れない = このアカウントの全ASINが「不明」。
            # 0埋めされないよう全件を失敗として記録する。
            print(f"[Amazon:{acc.name}] クライアント生成失敗: {e}", file=sys.stderr)
            failed_keys.update((asin, acc.name) for asin in asins_for_acc)
            continue

        acc_failed = 0
        for asin in asins_for_acc:
            try:
                metrics = fetch_order_metrics_with_retry(
                    client, asin, acc.name, interval_start, interval_end,
                )
            except Exception as e:  # noqa: BLE001 - 失敗内容を残して継続
                failed_keys.add((asin, acc.name))
                acc_failed += 1
                print(f"[Amazon:{acc.name}] ASIN {asin} 取得失敗: {e}", file=sys.stderr)
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
        print(f"[Amazon:{acc.name}] orderMetrics 取得完了 "
              f"({len(asins_for_acc) - acc_failed}/{len(asins_for_acc)} ASIN 成功"
              f"{f', {acc_failed}件失敗' if acc_failed else ''})",
              file=sys.stderr)

    return result, failed_keys


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
    col_a = with_retry(ws.col_values, 1)
    col_b = with_retry(ws.col_values, 2)
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
    with_retry(ws.update, range_name=rng, values=block)
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
            retry_batch_update(ws, chunk, value_input_option='USER_ENTERED')


def get_date_columns(ws) -> Dict[str, int]:
    """1行目から日付文字列→列番号のマップを返す（D列以降）。"""
    row1 = with_retry(ws.row_values, 1)
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
    failed_keys: Set[SalesKey] | None = None,
):
    """販売実績をシートへ書き込む。

    failed_keys (取得失敗) と「そもそも問い合わせていないペア」の日付セルは
    書き込みをスキップし、既存値をそのまま残す。
    「取得成功して本当に販売0」のときだけ 0 を書く。
    """
    failed_keys = failed_keys or set()
    # 実際に取得を試みたペア = 今回の在庫から作られた sku_map のキー
    attempted_keys: Set[SalesKey] = set(sku_map.keys())

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
    row1 = with_retry(ws.row_values, 1)
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
        retry_batch_update(ws, new_date_updates, value_input_option='USER_ENTERED')
        print(f"新規日付列 {len(new_date_updates)}件 を追加", file=sys.stderr)

    # 行番号マッピング
    key_to_row: Dict[SalesKey, int] = {k: idx for idx, k in enumerate(existing_keys, start=2)}

    # 取得に成功したペアだけ 0 or 販売数で埋める。
    # 取得失敗・未問い合わせのペアはスキップして既存値を保持する。
    updates = []
    skipped_failed: List[SalesKey] = []
    skipped_unqueried = 0
    for key in existing_keys:
        row = key_to_row[key]
        if key in failed_keys:
            skipped_failed.append(key)
            continue
        if key not in attempted_keys:
            # 在庫から消えた等で今回問い合わせていないペア。0埋めすると
            # 過去の実績を壊すのでスキップする。
            skipped_unqueried += 1
            continue
        for d in dates:
            if d not in date_cols:
                continue
            col = col_letter(date_cols[d])
            qty = sales.get((key[0], key[1], d), 0)
            updates.append({"range": f"{col}{row}", "values": [[qty]]})

    if skipped_failed:
        print(f"※ 取得失敗のため書き込みをスキップ (既存値を保持): "
              f"{len(skipped_failed)}ペア", file=sys.stderr)
        for (a, b) in sorted(skipped_failed):
            print(f"    スキップ: ASIN {a} / {b}", file=sys.stderr)
    if skipped_unqueried:
        print(f"※ 今回未問い合わせのため書き込みをスキップ: {skipped_unqueried}ペア",
              file=sys.stderr)

    if updates:
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            retry_batch_update(ws, chunk, value_input_option='USER_ENTERED')
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

    if not sku_map:
        print("エラー: 在庫から (ASIN,アカウント) を1件も取得できませんでした。"
              "0埋めを防ぐため中止します", file=sys.stderr)
        sys.exit(1)

    # 2. 販売実績を取得
    sales, failed_keys = fetch_sales(
        settings, list(sku_map.keys()), args.from_date, args.to_date)
    print(f"取得した販売レコード(>0): {len(sales)}件", file=sys.stderr)

    # 安全装置: 全ASINが取得失敗なら書き込まずに異常終了する。
    # (0埋めすると「売上0」と区別が付かず、正しい実績を破壊するため)
    if failed_keys and len(failed_keys) >= len(sku_map):
        print(f"エラー: 全 {len(sku_map)} ペアで orderMetrics 取得に失敗しました。"
              "0埋めを防ぐため書き込みを中止します", file=sys.stderr)
        sys.exit(1)
    if failed_keys:
        print(f"警告: {len(failed_keys)}ペアの取得に失敗 "
              f"(該当セルは書き込まず既存値を保持します)", file=sys.stderr)

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
    write_sales(sp, sales, sku_map, dates, failed_keys)

    url = f"https://docs.google.com/spreadsheets/d/{settings.google_spreadsheet_id}"
    print(f"\n完了 → {url}")
    if failed_keys:
        # 転記側や cron から失敗を検知できるよう非ゼロで終了する
        sys.exit(2)


if __name__ == "__main__":
    main()
