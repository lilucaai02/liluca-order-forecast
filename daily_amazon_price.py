#!/usr/bin/env python3
"""Amazon の実売単価を取得して、商品タブの「販売価格」行に記録する。

■ 何のためにあるか
販売価格そのものは在庫計算にも販売予想にも使っていない。唯一の使い道は
record_missing_timesales.py で「記録漏れのタイムセール」を見つけること。

    通常価格 = 直前90日の販売価格の最頻値
    セールをした日 = そこから一定%以上安い日

セールを見逃すと、その日の高い販売数が「ふだんの実力」として加重平均に
入り、予想が過大になる (加重平均は最新日に35%の重みがある)。
価格が途切れると、この検出ができなくなる。

■ 数字の出どころ
SP-API の getOrderMetrics (granularity=Day)。1日ぶんの実績として
unitCount (販売個数) と averageUnitPrice (平均単価) を返す。
これは商品ページの定価ではなく「実際に売れた価格の平均」なので、
クーポンや数量割引が使われた日はその分だけ下がる。

同じASINを複数アカウントで売っている場合は、売上金額と個数をそれぞれ
合算してから割る (単純平均だと販売数の少ないアカウントに引きずられる)。

■ 売れなかった日
単価が出ないので、直前に価格が付いた日の値をそのまま引き継ぐ。
最頻値を採るときに歯抜けがあると通常価格がぶれるため。

■ 安全装置
- 取得に失敗したASINは書き込まない (0や空欄で既存値を壊さない)
- 既に値が入っている日は上書きしない (--overwrite 指定時のみ上書き)
- 未来の日付には書かない

使い方:
  python3 daily_amazon_price.py --dry-run
  python3 daily_amazon_price.py                       # 直近14日の空欄を埋める
  python3 daily_amazon_price.py --days 90             # 遡る日数を変える
  python3 daily_amazon_price.py --days 30 --overwrite # 取得し直して上書き
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings                    # noqa: E402
from oshima_tab_blocks_config import OSHIMA_TAB_BLOCKS  # noqa: E402
from src.fetch_safety import sheets_retry, set_default_socket_timeout  # noqa: E402
from src.inventory import fetch_inventory               # noqa: E402
from src.sp_client import SPClient                      # noqa: E402
from daily_amazon_sales import fetch_order_metrics_with_retry  # noqa: E402

SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
SERIAL_BASE = datetime.date(1899, 12, 30)
LBL_PRICE = "販売価格"
DEFAULT_DAYS = 14


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def serial_to_date(v: Any) -> Optional[datetime.date]:
    try:
        return SERIAL_BASE + datetime.timedelta(days=int(v))
    except (TypeError, ValueError):
        return None


def build_asin_accounts(settings: Settings) -> Tuple[Dict[str, List[str]], List[str]]:
    """ASIN → そのASINを扱うアカウント名の一覧。在庫データから作る。"""
    by_asin: Dict[str, List[str]] = {}
    warn: List[str] = []
    ok = 0
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
        except Exception as e:                    # noqa: BLE001
            warn.append(f"[Amazon:{acc.name}] 在庫取得に失敗: {e}")
            continue
        ok += 1
        for it in items:
            if it.asin and acc.name not in by_asin.setdefault(it.asin, []):
                by_asin[it.asin].append(acc.name)
    if ok == 0:
        raise RuntimeError("全Amazonアカウントで在庫取得に失敗しました。中止します。")
    return by_asin, warn


def fetch_prices(settings: Settings, asins: List[str],
                 by_asin: Dict[str, List[str]],
                 start: datetime.date, end: datetime.date,
                 ) -> Tuple[Dict[str, Dict[datetime.date, float]], List[str]]:
    """ASIN → {日付: 実売単価}。失敗したASINは結果に入れない。"""
    iv_start = f"{start.isoformat()}T00:00:00+09:00"
    iv_end = f"{end.isoformat()}T23:59:59+09:00"

    # (asin, date) ごとに 売上金額と個数を貯めてから割る
    acc_units: Dict[Tuple[str, datetime.date], float] = {}
    acc_sales: Dict[Tuple[str, datetime.date], float] = {}
    failed: Dict[str, int] = {}
    warn: List[str] = []

    clients: Dict[str, Any] = {}
    for acc in settings.get_accounts():
        try:
            clients[acc.name] = SPClient(settings, account=acc)
        except Exception as e:                    # noqa: BLE001
            warn.append(f"[Amazon:{acc.name}] クライアント生成失敗: {e}")

    for asin in asins:
        for acc_name in by_asin.get(asin, []):
            client = clients.get(acc_name)
            if client is None:
                failed[asin] = failed.get(asin, 0) + 1
                continue
            try:
                metrics = fetch_order_metrics_with_retry(
                    client, asin, acc_name, iv_start, iv_end)
            except Exception as e:                # noqa: BLE001
                failed[asin] = failed.get(asin, 0) + 1
                warn.append(f"[Amazon:{acc_name}] ASIN {asin} 取得失敗: {str(e)[:80]}")
                continue
            for m in metrics:
                d = serial_iso(m.get("interval", ""))
                if d is None:
                    continue
                units = m.get("unitCount") or 0
                avg = (m.get("averageUnitPrice") or {}).get("amount")
                if not units or not avg:
                    continue
                k = (asin, d)
                acc_units[k] = acc_units.get(k, 0) + float(units)
                acc_sales[k] = acc_sales.get(k, 0) + float(units) * float(avg)

    out: Dict[str, Dict[datetime.date, float]] = {}
    for (asin, d), units in acc_units.items():
        if units > 0:
            out.setdefault(asin, {})[d] = acc_sales[(asin, d)] / units
    for asin, n in failed.items():
        warn.append(f"ASIN {asin}: {n}アカウントで取得に失敗したため書き込みません")
    return {a: v for a, v in out.items() if a not in failed}, warn


def serial_iso(interval: str) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(interval)[:10])
    except ValueError:
        return None


def main() -> None:
    p = argparse.ArgumentParser(description="Amazon実売単価 → 商品タブの販売価格行")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"何日前まで遡るか (既定 {DEFAULT_DAYS})")
    p.add_argument("--overwrite", action="store_true",
                   help="既に値が入っている日も上書きする")
    p.add_argument("--dry-run", action="store_true", help="書き込まず内容だけ表示")
    args = p.parse_args()

    set_default_socket_timeout()
    settings = Settings()
    today = datetime.date.today()
    start = today - datetime.timedelta(days=args.days)

    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = sheets_retry(gc.open_by_key, SPREADSHEET_ID)

    asins = sorted({str(b.get("asin", "")).strip()
                    for blocks in OSHIMA_TAB_BLOCKS.values() for b in blocks
                    if str(b.get("asin", "")).strip()})
    print(f"=== Amazon実売単価の取得 ({start} 〜 {today}) 対象 {len(asins)}商品 ===")

    by_asin, warn = build_asin_accounts(settings)
    prices, w2 = fetch_prices(settings, asins, by_asin, start, today)
    warn.extend(w2)
    print(f"価格を取得できた商品: {len(prices)}件")

    total_cells = 0
    for tab, blocks in OSHIMA_TAB_BLOCKS.items():
        ws = sheets_retry(sp.worksheet, tab)
        labels = sheets_retry(ws.col_values, 1)
        hdr = (sheets_retry(ws.get, "A1:ZZ1",
                            value_render_option="UNFORMATTED_VALUE") or [[]])[0]
        cols: Dict[datetime.date, int] = {}
        for j, v in enumerate(hdr, start=1):
            if not isinstance(v, (int, float)) or v < 40000:
                continue
            d = serial_to_date(v)
            if d:
                cols[d] = j
        targets = sorted(d for d in cols if start <= d <= today)
        if not targets:
            continue
        c0, c1 = cols[targets[0]], cols[targets[-1]]
        bounds = [b["asin_row"] for b in blocks] + [len(labels) + 2]

        rows, meta = [], []
        for bi, b in enumerate(blocks):
            lo, hi = bounds[bi], bounds[bi + 1]
            pr = next((x for x in range(lo, hi)
                       if x - 1 < len(labels)
                       and str(labels[x - 1]).strip() == LBL_PRICE), None)
            if pr is None:
                warn.append(f"{tab} {b['code']}: 「{LBL_PRICE}」行が見つかりません")
                continue
            # 直前の値を引き継ぐため、範囲の1列手前から読む
            rows.append(f"{col_letter(max(2, c0 - 1))}{pr}:{col_letter(c1)}{pr}")
            meta.append((b, pr, max(2, c0 - 1)))
        if not rows:
            continue
        got = sheets_retry(ws.batch_get, rows,
                           value_render_option="UNFORMATTED_VALUE")

        data = []
        for (b, pr, read_from), g in zip(meta, got):
            cur = list(g[0]) if g else []
            got_prices = prices.get(str(b.get("asin", "")).strip(), {})
            if not got_prices and not args.overwrite:
                continue
            vals, changed = [], 0
            last: Any = ""
            # 範囲の1列手前 = 直前の既存値
            if c0 - 1 >= 2 and cur and str(cur[0]).strip() != "":
                last = cur[0]
            for d in targets:
                j = cols[d]
                exist = cur[j - read_from] if j - read_from < len(cur) else ""
                if d in got_prices:
                    new = round(got_prices[d])
                    last = new
                elif str(exist).strip() != "":
                    new = exist
                    last = exist
                else:
                    new = last          # 売れなかった日は直前の価格を引き継ぐ
                if str(exist).strip() != "" and not args.overwrite:
                    new = exist         # 既存値は壊さない
                if str(new) != str(exist):
                    changed += 1
                vals.append(new)
            if changed:
                data.append({"range": f"{col_letter(c0)}{pr}:{col_letter(c1)}{pr}",
                             "values": [vals]})
                total_cells += changed
                print(f"  {tab} {b['code']}: {changed}日分 "
                      f"(実売単価 {len(got_prices)}日)")
        if data and not args.dry_run:
            sheets_retry(ws.batch_update, [dict(x) for x in data],
                         value_input_option="USER_ENTERED")

    if warn:
        print("\n--- 警告 ---")
        for x in warn:
            print(f"  [警告] {x}")
    if args.dry_run:
        print(f"\n[dry-run] {total_cells}セルを書き込む予定でした")
    else:
        print(f"\n→ {total_cells}セル書き込み完了")
    if not prices:
        sys.exit(1)


if __name__ == "__main__":
    main()
