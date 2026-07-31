#!/usr/bin/env python3
"""販売価格の下落から「記録漏れのタイムセール」を検出してイベント行へ記入する。

■ 背景
セラーセントラルでタイムセールを実施したのに アマゾンイベント 行が空欄のままだと、
その日の販売増が「通常日」として平均に混入し、予測のベース値
(直近7平日セール以外平均 / 直近セール以外加重平均) が実勢より高く出る。
2026-07-28・07-29 は 8タブ36商品で 13〜40% の値下げが行われていたのに
イベント行が空で、MP-03 では通常17個/日に対しベース値が32個/日まで上がり
「セールを止めた方が在庫が短くなる」という矛盾が出ていた。

■ 検出方法 (各ブロックごと)
  通常価格 = 対象期間の直前90日の 販売価格 の最頻値 (同率首位が複数なら中央値)
  対象日   = 販売価格が通常価格より --discount (既定5%) 以上安い日
             かつ アマゾンイベント 行が空欄の日 (既に入っている日は絶対に触らない)

■ 記入内容
  アマゾンイベント     : --label (既定「タイムセール」)
                        背景色は既存のタイムセール期間と同じ緑。
                        期間の先頭セルのみ黒文字、2日目以降は文字色=背景色の隠し文字。
  アマゾンイベント係数 : 実測係数 = 対象日の販売実績の平均 ÷ 開始前日のベース値
                        ベース値は「開始日の前日」の 直近7平日セール以外平均
                        (セール開始前の値なので汚染されていない)。
                        小数第2位まで。--coef-max (既定10) で上限。
                        ベース値が0、または対象日の販売平均が0で実測できない場合は
                        係数セルを変更しない (0 を書くと未来の仮置き係数を汚すため)。

■ 安全策
  - 販売実績 / 在庫実績 / 販売価格 など、他の行は一切変更しない。
  - 既にイベントが入っている日は上書きしない。
  - 行番号は必ずA列ラベルから解決する (config の行番号は決め打ちしない)。

使い方:
  python3 record_missing_timesales.py --from 2026-07-28 --to 2026-07-29 --dry-run
  python3 record_missing_timesales.py --from 2026-07-28 --to 2026-07-29
  python3 record_missing_timesales.py --from 2026-07-28 --to 2026-07-29 \
      --tab "マウスピース(在庫)"
"""

from __future__ import annotations

import argparse
import datetime
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
import oshima_tab_blocks_config
from src.fetch_safety import sheets_retry, set_default_socket_timeout

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
BASE = datetime.date(1899, 12, 30)

# A列ラベル (2026-07-29 に「長沼」を除去したあとの現行ラベル)
L_PRICE = "販売価格"
L_EVENT = "アマゾンイベント"
L_COEF = "アマゾンイベント係数"
L_SALES = "amazonFBA販売実績"
L_AVG7 = "直近7平日セール以外平均"
FIELDS = (("price", L_PRICE), ("event", L_EVENT), ("coef", L_COEF),
          ("sales", L_SALES), ("avg7", L_AVG7))

# 既存のタイムセール期間と同じ書式 (シートから読み取って確認済み: 純緑)
GREEN = {"red": 0, "green": 1, "blue": 0}
BLACK = {"red": 0, "green": 0, "blue": 0}

TABS = ["マウスピース(在庫)", "DS-01 (在庫) ", "TG-01(在庫)", "TG-02(在庫)",
        "GC-01(在庫)", "GC-02(在庫)", "PCI-01", "WB-01(在庫)", "WB-02",
        "TS-01", "PG-01"]

LOOKBACK = 90          # 通常価格を測る遡り日数
RANGES_PER_FETCH = 40  # 1回の batch_get にまとめるレンジ数


def col_letter(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def normal_price(price: dict, start: datetime.date) -> float | None:
    """開始日の直前 LOOKBACK 日の販売価格から通常価格 (最頻値) を求める。"""
    lo = start - datetime.timedelta(days=LOOKBACK)
    hist = [float(v) for d, v in price.items()
            if lo <= d < start and isinstance(v, (int, float)) and v > 0]
    if not hist:
        return None
    cnt: dict[float, int] = {}
    for v in hist:
        cnt[v] = cnt.get(v, 0) + 1
    mx = max(cnt.values())
    tops = sorted(k for k, c in cnt.items() if c == mx)
    return tops[0] if len(tops) == 1 else statistics.median(tops)


def rows_of_block(col_a: list, lo: int, hi: int) -> dict:
    out: dict[str, int] = {}
    for r in range(lo, hi):
        v = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
        if v and v not in out:
            out[v] = r
    return out


def process_tab(sp, tab: str, start: datetime.date, end: datetime.date,
                args, results: list) -> None:
    ws = sheets_retry(sp.worksheet, tab)
    row1 = (sheets_retry(ws.get, "1:1",
                         value_render_option="UNFORMATTED_VALUE") or [[]])[0]
    c2d = {i: BASE + datetime.timedelta(days=int(v))
           for i, v in enumerate(row1, 1) if isinstance(v, (int, float))}
    d2c = {d: c for c, d in c2d.items()}
    end_col = col_letter(max(c2d))
    col_a = sheets_retry(ws.col_values, 1)
    blocks = oshima_tab_blocks_config.get_blocks(tab)
    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]

    span = [start + datetime.timedelta(days=i)
            for i in range((end - start).days + 1)]
    missing_cols = [d for d in span if d not in d2c]
    if missing_cols:
        print(f"[{tab}] 対象日が1行目に無い: {missing_cols} → スキップ",
              file=sys.stderr)
        return

    # --- 行解決 + まとめ読み ---
    resolved, ranges = [], []
    for bi, b in enumerate(blocks):
        rmap = rows_of_block(col_a, bounds[bi], bounds[bi + 1])
        rows = {k: rmap.get(lab) for k, lab in FIELDS}
        resolved.append({"code": b["code"], "rows": rows})
        for k, lab in FIELDS:
            if rows[k]:
                ranges.append(f"'{tab}'!C{rows[k]}:{end_col}{rows[k]}")
            else:
                print(f"[{tab}] {b['code']}: ラベル '{lab}' が見つかりません",
                      file=sys.stderr)

    vals: dict[int, list] = {}
    for i in range(0, len(ranges), RANGES_PER_FETCH):
        chunk = ranges[i:i + RANGES_PER_FETCH]
        resp = sheets_retry(sp.values_batch_get, chunk,
                            params={"valueRenderOption": "UNFORMATTED_VALUE"})
        for rng, vr in zip(chunk, resp.get("valueRanges", [])):
            row = int(rng.split("!C")[1].split(":")[0])
            got = vr.get("values") or [[]]
            vals[row] = got[0] if got else []

    values, reqs = [], []

    def fmt(row: int, c: int, fg: dict) -> None:
        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": c - 1,
                      "endColumnIndex": c},
            "cell": {"userEnteredFormat": {
                "backgroundColor": GREEN,
                "textFormat": {"foregroundColor": fg}}},
            "fields": ("userEnteredFormat(backgroundColor,"
                       "textFormat.foregroundColor)")}})

    for rb in resolved:
        rows = rb["rows"]
        if not all(rows[k] for k, _ in FIELDS):
            continue

        def series(key: str) -> dict:
            out = {}
            for i, v in enumerate(vals.get(rows[key], [])):
                d = c2d.get(i + 3)
                if d is not None:
                    out[d] = v
            return out

        price, event = series("price"), series("event")
        sales, avg7 = series("sales"), series("avg7")

        np_ = normal_price(price, start)
        if not np_:
            continue

        def is_empty(d) -> bool:
            v = event.get(d)
            return v is None or str(v).strip() == ""

        days = [d for d in span
                if isinstance(price.get(d), (int, float))
                and price[d] > 0
                and price[d] <= np_ * (1 - args.discount)
                and is_empty(d)]
        if not days:
            continue

        # 実測係数 = 対象日の販売平均 ÷ 開始前日のベース値
        base = avg7.get(start - datetime.timedelta(days=1))
        sv = [float(sales[d]) for d in days
              if isinstance(sales.get(d), (int, float))]
        coef = None
        if isinstance(base, (int, float)) and base > 0 and sv and sum(sv) > 0:
            coef = min(round(sum(sv) / len(sv) / float(base), 2), args.coef_max)

        for d in days:
            c = d2c[d]
            prev = d - datetime.timedelta(days=1)
            # 期間の先頭 = 前日が同じラベルでない (前日を今回書いた場合も含めて判定)
            head = not (prev in days
                        or str(event.get(prev) or "").strip() == args.label)
            values.append({"range": f"{col_letter(c)}{rows['event']}",
                           "values": [[args.label]]})
            if coef:
                values.append({"range": f"{col_letter(c)}{rows['coef']}",
                               "values": [[coef]]})
            fmt(rows["event"], c, BLACK if head else GREEN)

        results.append({"tab": tab, "code": rb["code"],
                        "days": [d.isoformat() for d in days],
                        "normal_price": np_,
                        "prices": [price.get(d) for d in days],
                        "sales": [sales.get(d) for d in days],
                        "base": base, "coef": coef})
        print(f"  {tab[:16]:<18}{rb['code'][:20]:<22} "
              f"{[d.isoformat() for d in days]} 通常{np_:.0f}円→"
              f"{price[days[0]]:.0f}円 係数={coef if coef else '(実測不能→据置)'}",
              flush=True)

    if not values:
        print(f"[{tab}] 対象なし", flush=True)
        return
    if args.dry_run:
        print(f"[{tab}] dry-run: 値{len(values)}レンジ / 書式{len(reqs)}件 "
              f"(書き込みなし)", flush=True)
        return
    for i in range(0, len(values), 100):
        sheets_retry(ws.batch_update, [dict(x) for x in values[i:i + 100]],
                     value_input_option="USER_ENTERED")
    for i in range(0, len(reqs), 100):
        sheets_retry(sp.batch_update, {"requests": reqs[i:i + 100]})
    print(f"[{tab}] 書き込み完了 (値{len(values)}レンジ / 書式{len(reqs)}件)",
          flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="販売価格の下落から記録漏れのタイムセールを検出して記入する")
    ap.add_argument("--from", dest="dfrom", required=True,
                    help="対象開始日 YYYY-MM-DD")
    ap.add_argument("--to", dest="dto", required=True,
                    help="対象終了日 YYYY-MM-DD")
    ap.add_argument("--tab", action="append", help="対象タブ (複数可、既定は全11タブ)")
    ap.add_argument("--label", default="タイムセール", help="イベント行に書く文字列")
    ap.add_argument("--discount", type=float, default=0.05,
                    help="セールとみなす値下げ率 (既定 0.05 = 5%%)")
    ap.add_argument("--coef-max", type=float, default=10.0,
                    help="係数の上限 (既定 10)")
    ap.add_argument("--dry-run", action="store_true",
                    help="検出結果だけ表示してシートは書き換えない")
    args = ap.parse_args()

    start = datetime.datetime.strptime(args.dfrom, "%Y-%m-%d").date()
    end = datetime.datetime.strptime(args.dto, "%Y-%m-%d").date()
    if end < start:
        print("エラー: --to は --from 以降にしてください", file=sys.stderr)
        return 2

    set_default_socket_timeout(120.0)
    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env の GOOGLE_CREDENTIALS_FILE を確認してください",
              file=sys.stderr)
        return 2

    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = sheets_retry(gc.open_by_key, DEST_ID)

    tabs = args.tab or TABS
    results: list = []
    print(f"=== 記録漏れタイムセールの検出 [{start} 〜 {end}] "
          f"{len(tabs)}タブ ===", flush=True)
    for tab in tabs:
        process_tab(sp, tab, start, end, args, results)
        time.sleep(1.5)

    n_coef = sum(1 for r in results if r["coef"])
    print(f"\n対象商品 {len(results)}件 / 係数記入 {n_coef}件 "
          f"/ 係数据置 {len(results) - n_coef}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
