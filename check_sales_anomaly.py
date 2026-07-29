#!/usr/bin/env python3
"""発注予測シート(大島コピー)の日次異常検知。

毎朝の転記後に実行し、販売実績・在庫実績・予測ベースの異常を検出する。
2026-07 に発生した「SP-API の取得失敗が販売0としてシートを上書きし、
予測ベースが不当に下がる」障害 (commit 8bb0620) の再発を検知するのが主目的。

検知する異常:
  [1] 在庫減なのに販売0
      在庫実績が減っているのに同じ日の販売実績が 0 になっている。
      取得失敗の 0埋め、または転記漏れの典型的な症状。
      Amazon(FBA在庫実績) / 楽天(RSL在庫実績) / Yahoo(Stock Crew在庫実績) を確認。
  [2] 販売実績が連続0
      直近N日がすべて 0。ただし「元々売れていない商品」を除くため、
      その前の期間に販売実績がある商品に限る。
  [3] ベース値の急落
      「直近セール以外加重平均」が「直近7平日セール以外平均」の1/3未満。
      片方だけが壊れた実績を参照している場合に出る食い違い。
  [4] マイナス在庫
      各置き場所の予定行 (荒瀬倉庫 / 事務所 / イーウー中国 / 移動中 / 発注中 など)
      が負の値になっている。

使い方:
  python3 check_sales_anomaly.py                     # 全11タブを検査
  python3 check_sales_anomaly.py --quiet             # 異常時のみ出力 (cron向け)
  python3 check_sales_anomaly.py --tab "DS-01 (在庫) "
  python3 check_sales_anomaly.py --notify            # 異常時に ChatWork 通知
  python3 check_sales_anomaly.py --audit-days 365    # 過去1年の被害洗い出し(調査専用)

終了コード:
  0 = 異常なし
  1 = 異常あり
  2 = 検査自体に失敗 (シートが読めない等)

注意: 本スクリプトは読み取り専用。シートへの書き込みは一切行わない。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

# gspread / requests は既定でソケットタイムアウトを設定しないため、
# 応答が返ってこない接続を掴んだまま無限に待ち続けることがある。
# タイムアウトを入れて sheets_retry のリトライに載せる。
socket.setdefaulttimeout(120)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from oshima_tab_blocks_config import OSHIMA_TAB_BLOCKS
from src.fetch_safety import sheets_retry

DEST_SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
SERIAL_DATE_BASE = datetime.date(1899, 12, 30)

# --- ラベル (A列) ----------------------------------------------------------
# 2026-07-29 に「長沼」を除去したあとの現行ラベル
L_AMAZON_SALES = "amazonFBA販売実績"
L_AMAZON_STOCK = "FBA在庫実績"
L_RAKUTEN_SALES = "楽天販売実績"
L_RAKUTEN_STOCK = "RSL在庫実績"
L_YAHOO_SALES = "Yahoo販売実績"
L_YAHOO_STOCK = "Stock Crew在庫実績"
L_WEEKDAY_AVG = "直近7平日セール以外平均"
L_WEIGHTED_AVG = "直近セール以外加重平均"

# マイナスを検知する「予定」系の行ラベル (部分一致)
PLAN_ROW_MARKERS = ("在庫予定", "移動中予定", "発注中")

# チャネル定義: (表示名, configの販売行キー, configの在庫行キー)
CHANNELS: Sequence[Tuple[str, str, str]] = (
    ("Amazon", "sales_row", "stock_row"),
    ("楽天", "rakuten_sales_row", "rsl_stock_row"),
    ("Yahoo", "yahoo_sales_row", "stock_crew_stock_row"),
)

# 1回の batch_get にまとめるブロック数 (レスポンスが巨大になりすぎないよう分割)
BLOCKS_PER_FETCH = 6
# 1回の batch_get にまとめるレンジ数 (URL長を抑えるため)
RANGES_PER_FETCH = 30


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def to_number(v) -> Optional[float]:
    """数値化できれば float、空欄/エラー値なら None。

    重要: 空欄 (取得失敗の可能性) と 0 (取得成功して販売0) を区別するため、
    空欄は必ず None を返すこと。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s.startswith("#"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# シート構造の解決
# ---------------------------------------------------------------------------

def build_date_columns(ws) -> Dict[datetime.date, int]:
    """1行目の日付シリアル値 → 列番号 のマップを作る。"""
    raw = sheets_retry(ws.get, "A1:AMJ1", value_render_option="UNFORMATTED_VALUE")
    row1 = raw[0] if raw else []
    out: Dict[datetime.date, int] = {}
    for i, v in enumerate(row1, start=1):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 40000:
            out[SERIAL_DATE_BASE + datetime.timedelta(days=int(v))] = i
    return out


def resolve_block_rows(col_a: List[str], block: dict, lo: int, hi: int) -> dict:
    """A列ラベルを実際に読んで、ブロック内の各行番号を確定する。

    config の行番号は再構成のたびにずれる可能性があるため、ラベルで裏取りする。
    """
    def label(row: int) -> str:
        return col_a[row - 1].strip() if 0 < row <= len(col_a) else ""

    # ラベル → そのブロック内の全出現行
    occurrences: Dict[str, List[int]] = defaultdict(list)
    for r in range(lo, min(hi, len(col_a) + 1)):
        v = label(r)
        if v:
            occurrences[v].append(r)

    info: dict = {"code": block.get("code", "?"), "asin": block.get("asin", ""),
                  "lo": lo, "hi": hi, "rows": {}, "plan_rows": [], "warnings": []}

    def pick(config_key: str, expected_label: str) -> Optional[int]:
        """config の行番号を採用しつつ、ラベルが違えば実ラベルから引き直す。"""
        cfg = block.get(config_key)
        if cfg and label(cfg) == expected_label:
            return cfg
        cand = occurrences.get(expected_label, [])
        if len(cand) == 1:
            if cfg:
                info["warnings"].append(
                    f"config {config_key}={cfg} のラベルが '{label(cfg)}' → "
                    f"A列から '{expected_label}' = {cand[0]}行 を採用")
            return cand[0]
        if cfg and cand:
            info["warnings"].append(
                f"config {config_key}={cfg} のラベル不一致、候補 {cand} → スキップ")
        return None

    rows = info["rows"]
    rows["amazon_sales"] = pick("sales_row", L_AMAZON_SALES)
    rows["amazon_stock"] = pick("stock_row", L_AMAZON_STOCK)
    rows["rakuten_sales"] = pick("rakuten_sales_row", L_RAKUTEN_SALES)
    rows["rakuten_stock"] = pick("rsl_stock_row", L_RAKUTEN_STOCK)
    rows["yahoo_sales"] = pick("yahoo_sales_row", L_YAHOO_SALES)
    rows["yahoo_stock"] = pick("stock_crew_stock_row", L_YAHOO_STOCK)

    # 「直近7平日セール以外平均」「直近セール以外加重平均」はチャネルごとに
    # 同じラベルが3回現れる。各チャネルの販売実績行より前にある最後の出現を採る。
    wk7 = occurrences.get(L_WEEKDAY_AVG, [])
    wavg = occurrences.get(L_WEIGHTED_AVG, [])
    prev_bound = lo - 1
    for ch_key, sales_key in (("amazon", "amazon_sales"),
                              ("rakuten", "rakuten_sales"),
                              ("yahoo", "yahoo_sales")):
        sales_row = rows.get(sales_key)
        if not sales_row:
            continue
        seg7 = [r for r in wk7 if prev_bound < r < sales_row]
        segw = [r for r in wavg if prev_bound < r < sales_row]
        rows[f"{ch_key}_wk7"] = seg7[-1] if seg7 else None
        rows[f"{ch_key}_wavg"] = segw[-1] if segw else None
        prev_bound = sales_row

    # 置き場所の予定行 (マイナス検知対象)
    for lbl, rs in occurrences.items():
        if any(m in lbl for m in PLAN_ROW_MARKERS):
            for r in rs:
                info["plan_rows"].append((lbl, r))
    info["plan_rows"].sort(key=lambda x: x[1])

    return info


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------

def fetch_block_matrix(ws, block_infos: List[dict], c_from: int, c_to: int
                       ) -> Dict[int, Dict[int, List]]:
    """各ブロックの必要行範囲を列 c_from..c_to で取得する。

    返り値: {ブロックindex: {行番号: [値, ...]}}  値は c_from 起点で 0 埋め済み
    """
    width = c_to - c_from + 1
    cA, cB = col_letter(c_from), col_letter(c_to)

    result: Dict[int, Dict[int, List]] = {}
    todo: List[Tuple[int, int, int]] = []  # (block_idx, row_lo, row_hi)
    for bi, info in enumerate(block_infos):
        needed = [r for r in info["rows"].values() if r]
        needed += [r for _l, r in info["plan_rows"]]
        if not needed:
            result[bi] = {}
            continue
        todo.append((bi, min(needed), max(needed)))

    for i in range(0, len(todo), BLOCKS_PER_FETCH):
        chunk = todo[i:i + BLOCKS_PER_FETCH]
        ranges = [f"{cA}{lo}:{cB}{hi}" for (_bi, lo, hi) in chunk]
        got = sheets_retry(ws.batch_get, ranges,
                           value_render_option="UNFORMATTED_VALUE")
        for (bi, lo, hi), grid in zip(chunk, got):
            grid = list(grid or [])
            by_row: Dict[int, List] = {}
            for off in range(hi - lo + 1):
                row = list(grid[off]) if off < len(grid) else []
                if len(row) < width:
                    row = row + [""] * (width - len(row))
                by_row[lo + off] = row
            result[bi] = by_row
        time.sleep(1.0)
    return result


def fetch_rows_matrix(ws, row_list: List[int], c_from: int, c_to: int
                      ) -> Dict[int, List]:
    """指定した行だけを列 c_from..c_to で取得する (過去監査用)。"""
    width = c_to - c_from + 1
    cA, cB = col_letter(c_from), col_letter(c_to)
    out: Dict[int, List] = {}
    rows = sorted(set(row_list))
    for i in range(0, len(rows), RANGES_PER_FETCH):
        chunk = rows[i:i + RANGES_PER_FETCH]
        ranges = [f"{cA}{r}:{cB}{r}" for r in chunk]
        got = sheets_retry(ws.batch_get, ranges,
                           value_render_option="UNFORMATTED_VALUE")
        for r, grid in zip(chunk, got):
            row = list(grid[0]) if grid and grid[0] else []
            if len(row) < width:
                row = row + [""] * (width - len(row))
            out[r] = row
        time.sleep(1.0)
    return out


# ---------------------------------------------------------------------------
# 異常判定
# ---------------------------------------------------------------------------

class Anomaly:
    __slots__ = ("kind", "tab", "code", "channel", "item", "detail", "sort_key")

    def __init__(self, kind, tab, code, channel, item, detail, sort_key=0.0):
        self.kind = kind
        self.tab = tab
        self.code = code
        self.channel = channel
        self.item = item
        self.detail = detail
        self.sort_key = sort_key


def series(by_row: Dict[int, List], row: Optional[int], c_from: int,
           dates: List[datetime.date], date_cols: Dict[datetime.date, int]
           ) -> Dict[datetime.date, Optional[float]]:
    """行データを {日付: 数値 or None} に変換。"""
    out: Dict[datetime.date, Optional[float]] = {}
    if not row or row not in by_row:
        return out
    vals = by_row[row]
    for d in dates:
        c = date_cols.get(d)
        if c is None:
            continue
        idx = c - c_from
        out[d] = to_number(vals[idx]) if 0 <= idx < len(vals) else None
    return out


def check_stock_down_no_sales(sales, stock, days: List[datetime.date],
                              min_drop: float) -> List[Tuple[datetime.date, float]]:
    """在庫が減っているのに販売0の日を返す。

    在庫実績は毎朝のスナップショット。つまり d 日の販売は
    「d 日の在庫 → d+1 日の在庫」の減少として現れる。
    """
    hits = []
    for d in days:
        s = sales.get(d)
        if s is None or s != 0:
            continue
        st_d = stock.get(d)
        st_n = stock.get(d + datetime.timedelta(days=1))
        if st_d is None or st_n is None:
            continue
        # Yahoo の -1 は「無制限」なので在庫数として比較しない
        if st_d < 0 or st_n < 0:
            continue
        drop = st_d - st_n
        if drop >= min_drop:
            hits.append((d, drop))
    return hits


def scan_tab(ws, tab: str, blocks: List[dict], today: datetime.date,
             args) -> Tuple[List[Anomaly], List[str]]:
    """1タブを検査して異常リストを返す。"""
    warnings: List[str] = []
    if args.verbose:
        print(f"  [{tab}] A列ラベル読み込み中...", file=sys.stderr)
    col_a = sheets_retry(ws.col_values, 1)
    if args.verbose:
        print(f"  [{tab}] 日付列読み込み中...", file=sys.stderr)
    date_cols = build_date_columns(ws)
    if not date_cols:
        warnings.append(f"[{tab}] 1行目に日付列が見つかりません")
        return [], warnings

    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    infos = [resolve_block_rows(col_a, b, bounds[i], bounds[i + 1])
             for i, b in enumerate(blocks)]
    for info in infos:
        for w in info["warnings"]:
            warnings.append(f"[{tab}/{info['code']}] {w}")

    # 取得する列範囲: 過去 history 日 〜 未来 plan 日
    d_start = today - datetime.timedelta(days=args.history_days)
    d_end = today + datetime.timedelta(days=args.plan_days)
    cand = [c for d, c in date_cols.items() if d_start <= d <= d_end]
    if not cand:
        warnings.append(f"[{tab}] 対象期間の日付列がありません")
        return [], warnings
    c_from, c_to = min(cand), max(cand)

    if args.verbose:
        print(f"  [{tab}] データ取得中 ({len(infos)}ブロック / "
              f"{col_letter(c_from)}〜{col_letter(c_to)}列)...", file=sys.stderr)
    matrices = fetch_block_matrix(ws, infos, c_from, c_to)

    # 判定に使う日付リスト
    past_days = [d_start + datetime.timedelta(days=i)
                 for i in range((today - d_start).days + 1)]
    recent = [today - datetime.timedelta(days=i)
              for i in range(args.days, 0, -1)]           # 直近N日 (今日を含まない)
    baseline = [d for d in past_days if d < recent[0]]    # それ以前
    future = [today + datetime.timedelta(days=i) for i in range(args.plan_days + 1)]

    out: List[Anomaly] = []
    for bi, info in enumerate(infos):
        by_row = matrices.get(bi, {})
        code = info["code"]

        for ch_name, sales_key, stock_key in CHANNELS:
            pfx = {"Amazon": "amazon", "楽天": "rakuten", "Yahoo": "yahoo"}[ch_name]
            r_sales = info["rows"].get(f"{pfx}_sales")
            r_stock = info["rows"].get(f"{pfx}_stock")
            if not r_sales:
                continue
            s_all = series(by_row, r_sales, c_from, past_days, date_cols)
            st_all = series(by_row, r_stock, c_from, past_days + [today], date_cols)

            # --- [1] 在庫減なのに販売0 ---
            if r_stock:
                hits = check_stock_down_no_sales(
                    s_all, st_all, recent, args.min_drop)
                for d, drop in hits:
                    out.append(Anomaly(
                        "在庫減なのに販売0", tab, code, ch_name,
                        d.isoformat(),
                        f"販売実績=0 なのに在庫が {drop:.0f} 減少 "
                        f"({d.isoformat()} → {(d + datetime.timedelta(days=1)).isoformat()})",
                        sort_key=-drop))

            # --- [2] 販売実績が連続0 ---
            rec_vals = [s_all.get(d) for d in recent]
            if rec_vals and all(v == 0 for v in rec_vals):
                base_vals = [s_all.get(d) for d in baseline]
                base_sum = sum(v for v in base_vals if v)
                if base_sum > 0:
                    out.append(Anomaly(
                        "販売実績が連続0", tab, code, ch_name,
                        f"{recent[0].isoformat()}〜{recent[-1].isoformat()}",
                        f"直近{args.days}日すべて0 "
                        f"(その前{len(baseline)}日は計 {base_sum:.0f}個 販売あり)",
                        sort_key=-base_sum))

            # --- [3] ベース値の急落 ---
            r_wk7 = info["rows"].get(f"{pfx}_wk7")
            r_wavg = info["rows"].get(f"{pfx}_wavg")
            if r_wk7 and r_wavg:
                v7 = series(by_row, r_wk7, c_from, [today], date_cols).get(today)
                vw = series(by_row, r_wavg, c_from, [today], date_cols).get(today)
                if (v7 is not None and vw is not None
                        and v7 >= args.base_min and vw < v7 / 3.0):
                    out.append(Anomaly(
                        "ベース値の急落", tab, code, ch_name, today.isoformat(),
                        f"直近セール以外加重平均={vw:.2f} が "
                        f"直近7平日セール以外平均={v7:.2f} の1/3未満 "
                        f"(比 {vw / v7:.2f})",
                        sort_key=vw / v7))

        # --- [4] マイナス在庫 ---
        for lbl, r in info["plan_rows"]:
            vals = series(by_row, r, c_from, future, date_cols)
            neg = [(d, v) for d, v in vals.items() if v is not None and v < 0]
            if neg:
                neg.sort(key=lambda x: x[0])
                worst = min(neg, key=lambda x: x[1])
                out.append(Anomaly(
                    "マイナス在庫", tab, code, "-", lbl,
                    f"{neg[0][0].isoformat()} から負 "
                    f"(最小 {worst[1]:.0f} @ {worst[0].isoformat()}, {len(neg)}日分)",
                    sort_key=worst[1]))

    return out, warnings


# ---------------------------------------------------------------------------
# 過去監査 (タスク3: 調査専用。データは一切変更しない)
# ---------------------------------------------------------------------------

def audit_tab(ws, tab: str, blocks: List[dict], today: datetime.date,
              days: int, min_drop: float) -> List[Tuple[str, str, str, datetime.date, float]]:
    """過去 days 日の「在庫減なのに販売0」を洗い出す。

    返り値: [(tab, code, channel, date, drop), ...]
    """
    col_a = sheets_retry(ws.col_values, 1)
    date_cols = build_date_columns(ws)
    if not date_cols:
        return []
    bounds = [b["asin_row"] for b in blocks] + [len(col_a) + 2]
    infos = [resolve_block_rows(col_a, b, bounds[i], bounds[i + 1])
             for i, b in enumerate(blocks)]

    d_start = today - datetime.timedelta(days=days)
    cand = [c for d, c in date_cols.items() if d_start <= d <= today]
    if not cand:
        return []
    c_from, c_to = min(cand), max(cand)

    wanted: List[int] = []
    for info in infos:
        for pfx in ("amazon", "rakuten", "yahoo"):
            for suf in ("sales", "stock"):
                r = info["rows"].get(f"{pfx}_{suf}")
                if r:
                    wanted.append(r)
    rows_data = fetch_rows_matrix(ws, wanted, c_from, c_to)

    days_list = [d_start + datetime.timedelta(days=i)
                 for i in range((today - d_start).days + 1)]

    out = []
    for info in infos:
        for ch_name in ("Amazon", "楽天", "Yahoo"):
            pfx = {"Amazon": "amazon", "楽天": "rakuten", "Yahoo": "yahoo"}[ch_name]
            r_sales = info["rows"].get(f"{pfx}_sales")
            r_stock = info["rows"].get(f"{pfx}_stock")
            if not r_sales or not r_stock:
                continue
            s = series(rows_data, r_sales, c_from, days_list, date_cols)
            st = series(rows_data, r_stock, c_from, days_list, date_cols)
            for d, drop in check_stock_down_no_sales(s, st, days_list[:-1], min_drop):
                out.append((tab, info["code"], ch_name, d, drop))
    return out


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

KIND_ICON = {
    "在庫減なのに販売0": "🔴",
    "販売実績が連続0": "🟠",
    "ベース値の急落": "🟡",
    "マイナス在庫": "🔵",
}
KIND_ORDER = ["在庫減なのに販売0", "販売実績が連続0", "ベース値の急落", "マイナス在庫"]


def format_report(anomalies: List[Anomaly], warnings: List[str],
                  today: datetime.date, args) -> str:
    lines: List[str] = []
    lines.append("=" * 74)
    lines.append(f" 日次異常検知レポート  {today.isoformat()}")
    lines.append("=" * 74)

    if not anomalies:
        lines.append("")
        lines.append("  異常はありませんでした。")
    else:
        by_kind: Dict[str, List[Anomaly]] = defaultdict(list)
        for a in anomalies:
            by_kind[a.kind].append(a)
        lines.append("")
        lines.append(f"  合計 {len(anomalies)} 件の異常を検出")
        for k in KIND_ORDER:
            if by_kind.get(k):
                lines.append(f"    {KIND_ICON[k]} {k}: {len(by_kind[k])}件")

        for k in KIND_ORDER:
            items = by_kind.get(k)
            if not items:
                continue
            lines.append("")
            lines.append(f"{KIND_ICON[k]} 【{k}】 {len(items)}件")
            lines.append("-" * 74)
            items.sort(key=lambda a: (a.sort_key, a.tab, a.code))
            for a in items:
                head = f"  {a.code} ({a.tab})"
                if a.channel and a.channel != "-":
                    head += f" / {a.channel}"
                lines.append(head)
                lines.append(f"      {a.item}: {a.detail}")

    if warnings:
        lines.append("")
        lines.append(f"⚠ 検査上の注意 {len(warnings)}件")
        lines.append("-" * 74)
        for w in warnings:
            lines.append(f"  {w}")

    lines.append("")
    lines.append("-" * 74)
    lines.append(f" 判定条件: 在庫減の閾値={args.min_drop:.0f}個 / "
                 f"連続0の期間={args.days}日 / ベース値下限={args.base_min} / "
                 f"予定行の先読み={args.plan_days}日")
    lines.append(f" 対象: {DEST_SPREADSHEET_ID}")
    lines.append("=" * 74)
    return "\n".join(lines)


def format_chatwork(anomalies: List[Anomaly], today: datetime.date) -> str:
    by_kind: Dict[str, List[Anomaly]] = defaultdict(list)
    for a in anomalies:
        by_kind[a.kind].append(a)
    out = [f"[info][title]⚠ 販売データ異常検知 ({today.isoformat()})[/title]"]
    out.append(f"合計 {len(anomalies)} 件")
    for k in KIND_ORDER:
        items = by_kind.get(k)
        if not items:
            continue
        out.append("")
        out.append(f"{KIND_ICON[k]} [b]{k}[/b] ({len(items)}件)")
        items.sort(key=lambda a: (a.sort_key, a.tab, a.code))
        for a in items[:12]:
            ch = f" {a.channel}" if a.channel and a.channel != "-" else ""
            out.append(f"  ・{a.code}{ch} {a.item}: {a.detail}")
        if len(items) > 12:
            out.append(f"  ...他 {len(items) - 12} 件")
    out.append("[/info]")
    return "\n".join(out)


def post_to_chatwork(token: str, room_id: str, body: str) -> dict:
    url = f"https://api.chatwork.com/v2/rooms/{room_id}/messages"
    data = urllib.parse.urlencode({"body": body, "self_unread": "1"}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"X-ChatWorkToken": token,
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------

def open_spreadsheet(settings: Settings):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    return sheets_retry(gc.open_by_key, DEST_SPREADSHEET_ID)


def run_audit(sp, tabs: List[str], today: datetime.date, args) -> int:
    """過去 N 日の「在庫減なのに販売0」を洗い出して集計表示する (調査専用)。"""
    all_hits: List[Tuple[str, str, str, datetime.date, float]] = []
    for tab in tabs:
        blocks = OSHIMA_TAB_BLOCKS[tab]
        try:
            ws = sheets_retry(sp.worksheet, tab)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ [{tab}] シート取得失敗: {e}", file=sys.stderr)
            continue
        hits = audit_tab(ws, tab, blocks, today, args.audit_days, args.min_drop)
        print(f"  [{tab}] {len(hits)}件", file=sys.stderr)
        all_hits.extend(hits)
        time.sleep(1.5)

    print("=" * 74)
    print(f" 過去{args.audit_days}日の「在庫減なのに販売0」洗い出し "
          f"({(today - datetime.timedelta(days=args.audit_days)).isoformat()} 〜 {today.isoformat()})")
    print("=" * 74)
    print(f"\n 合計 {len(all_hits)} 件\n")

    if all_hits:
        by_month: Dict[str, int] = defaultdict(int)
        by_month_ch: Dict[Tuple[str, str], int] = defaultdict(int)
        by_item: Dict[Tuple[str, str, str], List[datetime.date]] = defaultdict(list)
        for tab, code, ch, d, _drop in all_hits:
            by_month[d.strftime("%Y-%m")] += 1
            by_month_ch[(d.strftime("%Y-%m"), ch)] += 1
            by_item[(tab, code, ch)].append(d)

        print("■ 月別件数")
        print(f"  {'月':<10}{'件数':>6}   内訳")
        for m in sorted(by_month):
            det = "  ".join(f"{ch}:{by_month_ch[(m, ch)]}"
                            for ch in ("Amazon", "楽天", "Yahoo")
                            if by_month_ch.get((m, ch)))
            print(f"  {m:<10}{by_month[m]:>6}   {det}")

        print("\n■ 商品別件数 (多い順)")
        print(f"  {'商品コード':<22}{'チャネル':<8}{'件数':>5}  期間")
        for (tab, code, ch), ds in sorted(by_item.items(),
                                          key=lambda kv: -len(kv[1])):
            ds.sort()
            print(f"  {code:<22}{ch:<8}{len(ds):>5}  "
                  f"{ds[0].isoformat()} 〜 {ds[-1].isoformat()}  [{tab}]")

        print("\n■ 連続被害期間 (同一商品×チャネルで3日以上連続)")
        found_streak = False
        for (tab, code, ch), ds in sorted(by_item.items()):
            ds = sorted(set(ds))
            run = [ds[0]]
            for prev, cur in zip(ds, ds[1:]):
                if (cur - prev).days == 1:
                    run.append(cur)
                else:
                    if len(run) >= 3:
                        print(f"  {code} / {ch}: {run[0].isoformat()} 〜 "
                              f"{run[-1].isoformat()} ({len(run)}日連続)")
                        found_streak = True
                    run = [cur]
            if len(run) >= 3:
                print(f"  {code} / {ch}: {run[0].isoformat()} 〜 "
                      f"{run[-1].isoformat()} ({len(run)}日連続)")
                found_streak = True
        if not found_streak:
            print("  なし")

        print("\n■ 修正候補リスト (再取得すべき 商品×チャネル×日付)")
        print("  ※ 本スクリプトはデータを一切変更しません。再取得は別途手動で。")
        for (tab, code, ch), ds in sorted(by_item.items()):
            print(f"  {code} / {ch}: " + ", ".join(d.isoformat() for d in sorted(ds)))

    print("\n" + "=" * 74)
    return 1 if all_hits else 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="発注予測シート(大島コピー)の日次異常検知 (読み取り専用)")
    p.add_argument("--tab", action="append",
                   help="検査するタブ (複数可。既定は全11タブ)")
    p.add_argument("--date", help="基準日 YYYY-MM-DD (既定: 今日)")
    p.add_argument("--days", type=int, default=7,
                   help="直近何日を検査するか (既定 7)")
    p.add_argument("--history-days", type=int, default=37,
                   help="「元々売れていたか」を見る遡り日数 (既定 37)")
    p.add_argument("--plan-days", type=int, default=60,
                   help="マイナス在庫を何日先まで見るか (既定 60)")
    p.add_argument("--min-drop", type=float, default=1.0,
                   help="在庫減とみなす最小減少数 (既定 1)")
    p.add_argument("--base-min", type=float, default=1.0,
                   help="ベース値急落を判定する下限 (既定 1.0)")
    p.add_argument("--quiet", action="store_true",
                   help="異常があるときだけ出力する")
    p.add_argument("--verbose", action="store_true",
                   help="進捗を標準エラーに詳しく出す")
    p.add_argument("--notify", action="store_true",
                   help="異常時に ChatWork へ通知する")
    p.add_argument("--audit-days", type=int,
                   help="過去N日の被害を洗い出す調査モード (通常検査は行わない)")
    args = p.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env の GOOGLE_CREDENTIALS_FILE を確認してください",
              file=sys.stderr)
        return 2

    today = (datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
             if args.date else datetime.date.today())

    tabs = args.tab or list(OSHIMA_TAB_BLOCKS.keys())
    unknown = [t for t in tabs if t not in OSHIMA_TAB_BLOCKS]
    if unknown:
        print(f"エラー: 未登録のタブ {unknown}", file=sys.stderr)
        print(f"登録済み: {list(OSHIMA_TAB_BLOCKS.keys())}", file=sys.stderr)
        return 2

    try:
        sp = open_spreadsheet(settings)
    except Exception as e:  # noqa: BLE001
        print(f"エラー: スプレッドシートを開けません: {e}", file=sys.stderr)
        return 2

    if args.audit_days:
        return run_audit(sp, tabs, today, args)

    n_blocks = sum(len(OSHIMA_TAB_BLOCKS[t]) for t in tabs)
    print(f"=== 日次異常検知 [{today.isoformat()}] "
          f"{len(tabs)}タブ / {n_blocks}ブロック ===", file=sys.stderr)

    anomalies: List[Anomaly] = []
    warnings: List[str] = []
    failed_tabs: List[str] = []
    for tab in tabs:
        try:
            ws = sheets_retry(sp.worksheet, tab)
            found, warns = scan_tab(ws, tab, OSHIMA_TAB_BLOCKS[tab], today, args)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ [{tab}] 検査失敗: {e}", file=sys.stderr)
            failed_tabs.append(tab)
            continue
        print(f"  [{tab}] {len(found)}件の異常", file=sys.stderr)
        anomalies.extend(found)
        warnings.extend(warns)
        time.sleep(1.5)

    if failed_tabs:
        warnings.append(f"検査できなかったタブ: {', '.join(failed_tabs)}")

    if not args.quiet or anomalies:
        print(format_report(anomalies, warnings, today, args))

    if args.notify and anomalies:
        token = settings.chatwork_api_token
        room = settings.chatwork_room_id
        if not token or not room:
            print("※ ChatWork 設定 (CHATWORK_API_TOKEN / CHATWORK_ROOM_ID) が"
                  "無いため通知はスキップしました", file=sys.stderr)
        else:
            try:
                res = post_to_chatwork(token, room,
                                       format_chatwork(anomalies, today))
                print(f"✓ ChatWork 通知しました "
                      f"(message_id={res.get('message_id')})", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"✗ ChatWork 通知に失敗: {e}", file=sys.stderr)

    if failed_tabs:
        return 2
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main())
