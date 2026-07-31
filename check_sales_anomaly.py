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

あわせて「在庫アラート」も集計する (データ異常ではなく供給判断の材料):
  ダッシュボード２を読み、緊急度順に分類する。
    ⚠ 未着            「対応済み」に日付があるのに20日を過ぎても在庫が戻らない
                       (最優先。冒頭に出す)
    🛑 在庫切れ        FBA在庫0 / 総在庫0 / FBA在庫切れ予測日が今日以前
    ⚠️ 危険(在庫僅少)  FBA残り日数が1〜15日
    📦 梱包            「FBA梱包必要数」が0より大きい商品
    🔽 販売調整        シート側「販売調整」列 (🛑セール全停止 / 🔽セール減らす)
    🏭 発注            「発注中個数」が「発注個数予測」に足りていない商品
    📦 要発注(至急)    総数発注残り日数が ✖️ またはマイナス (上の発注に出た分は除く)
    📦 要発注(30日以内) 総数発注残り日数が0〜30日
    【発注済み・様子見】上の要発注のうち 発注中個数 ≧ 発注個数予測 のもの
  梱包・発注はシートに専用の文言列を持たない (2026-07-31 に「梱包」「発注」列を
  削除)。必要総量そのものである既存の数値列から通知側で組み立てる。
  「対応済み」列に日付を入れた商品 (✓ 済) は通知から丸ごと除外する。
  梱包・発送して入庫見込みが立っているのにアラートが出続けるのを止めるため。
  20日を過ぎても在庫が戻らなければ「⚠ 未着」として先頭に復活する。

  在庫が30日分になった時点で補充が到着している想定で運用しているため、
  FBA残り日数が30日を切っている = 予定どおりなら入庫しているはずの時点で
  入庫していない、というサイン。1ヶ月の猶予があればセールを調整して延ばせる。
  入庫の見込みが立っていれば無視してよい判断材料なので、異常件数や終了コードには
  含めない (通知には含める)。
  文言・色・日数は stock_alert_labels.py に集約 (シート側と共通の定数)。

ChatWork通知は2通に分けて送る (長いと読まれないため):
  1通目 📊 在庫アラート     … 上の在庫セクション
  2通目 🔧 販売データ異常   … 下の [1]〜[4]
  どちらか一方が0件ならその通は送らない。--only で片方だけにもできる。
  送信先は CHATWORK_ROOM_ID_STOCK / CHATWORK_ROOM_ID_DATA で分けられる
  (未設定なら CHATWORK_ROOM_ID にフォールバック)。

使い方:
  python3 check_sales_anomaly.py                     # 全11タブを検査
  python3 check_sales_anomaly.py --quiet             # 異常時のみ出力 (cron向け)
  python3 check_sales_anomaly.py --tab "DS-01 (在庫) "
  python3 check_sales_anomaly.py --notify            # ChatWork へ2通 通知
  python3 check_sales_anomaly.py --print-body        # 送信本文を表示するだけ
  python3 check_sales_anomaly.py --notify --only stock   # 在庫アラートのみ送信
  python3 check_sales_anomaly.py --no-stock-alert    # 在庫アラートの集計をスキップ
  python3 check_sales_anomaly.py --audit-days 365    # 過去1年の被害洗い出し(調査専用)

終了コード:
  0 = 異常なし
  1 = 異常あり (在庫アラートのみの場合は 0。あくまで判断材料のため)
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
# アラートの文言・日数はシート生成側と同じ定数を使う (食い違い防止)
from stock_alert_labels import (
    COLUMN_ORDER,
    COLUMN_SPECS,
    DONE_VALID_DAYS,
    H_DONE,
    K_DONE,
    K_OVERDUE,
    LEGACY_ALERT_HEADER,
    SECTION_ORDER,
    SECTION_TITLES,
    classify,
)

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

# --- ダッシュボード２ -------------------------------------------------------
DASHBOARD_TAB = "ダッシュボード２"

# 参照するヘッダー文字列 (列位置は必ず1行目から解決する。決め打ちしない)。
# ヘッダーは改行を含むものがあるため、1行目だけを見て前方一致で照合する。
DH_ORDER_DAYS = "総数発注残り日数"
DH_DAYS = "FBA残り日数"
# シート側に文言が入るアラート列 (現行は「販売調整」だけ)。
# ✓済 / ⚠未着 もこの列に出る。
DH_ACT = {key: COLUMN_SPECS[key][0] for key in COLUMN_ORDER}
DH_DONE = H_DONE
DH_HINT = "調整の目安"
DH_FBA_OUT_DATE = "FBA在庫切れ予測日"
DH_ORDER_DATE = "総数発注予測日"
DH_WEEKDAY_SALES = "平日販売数合計"
DH_FBA_STOCK = "FBA在庫数"
DH_RSL_STOCK = "RSL在庫数"
DH_SC_STOCK = "ストッククルー在庫数"
DH_ORDER_QTY = "発注個数予測"
DH_ORDERING = "発注中個数"
DH_PACK_NEED = "FBA梱包必要数"    # 📦 梱包セクションの出典 (旧「梱包」列の代わり)
DH_LEAD_TIME = "リードタイム"
# 「総在庫」はヘッダーが空欄の列。総数発注予測日 と 平日販売数合計 の間にある。

DAYS_DANGER = 15   # FBA残り日数がこれ以下なら「危険 (在庫僅少)」
DAYS_ORDER_SOON = 30   # 総数発注残り日数がこれ以下なら「要発注 (30日以内)」

# ChatWork通知の各セクションに載せる上限 (超えた分は件数のみ)
MAX_PER_SECTION = 10

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
# ダッシュボード２ (未着 / 在庫切れ / 在庫僅少 / 梱包 / 販売調整 / 発注 / 要発注)
#   判定はシート側の数式が出した結果を読むだけ。書き込みは一切しない。
#   列位置は必ず1行目のヘッダーから解決する (並べ替えられても壊れないように)。
# ---------------------------------------------------------------------------

def norm_header(h) -> str:
    """ヘッダーを正規化する。改行入りのヘッダーは1行目だけを使う。

    例: '総数発注予測日\\n(在庫切れ30日前-リードタイム)' → '総数発注予測日'
    """
    return str(h).split("\n")[0].strip()


def find_col(header: List[str], name: str) -> Optional[int]:
    """正規化済みヘッダーから列インデックスを引く (完全一致 → 前方一致)。"""
    for i, h in enumerate(header):
        if h == name:
            return i
    for i, h in enumerate(header):
        if h and h.startswith(name):
            return i
    return None


def to_date(v, today: Optional[datetime.date] = None) -> Optional[datetime.date]:
    """ダッシュボード２の日付セルを date に変換する。

    予測日の列 (FBA在庫切れ予測日 / 総数発注予測日 など) は日付値ではなく
    数式が組み立てた「文字列」が入っている:
        IF(YEAR(x)=YEAR(TODAY()), TEXT(x,"M/D"), TEXT(x,"YYYY/M/D"))
    つまり今年なら "8/24"、来年以降なら "2027/3/10" になる。
    年が省略されている場合は基準日の年として解釈する。
    日付シリアル値 (数値) が入っていた場合にも対応しておく。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (SERIAL_DATE_BASE + datetime.timedelta(days=int(v))
                if v >= 20000 else None)
    s = str(v).strip().replace("-", "/")
    if not s:
        return None
    parts = s.split("/")
    try:
        if len(parts) == 2:
            year = (today or datetime.date.today()).year
            return datetime.date(year, int(parts[0]), int(parts[1]))
        if len(parts) == 3:
            return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        return None
    return None


class DashRow:
    """ダッシュボード２の1行 (1商品)。"""
    __slots__ = ("name", "order_days", "order_days_text", "fba_days",
                 "acts", "done_date", "hint", "fba_out_date", "order_date",
                 "total", "fba", "rsl", "sc", "order_qty", "ordering",
                 "pack_need", "lead")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        if self.acts is None:
            self.acts = {}

    def act(self, key: str) -> str:
        """アラート列 (販売調整) のセル文字列 (空欄なら "")。"""
        return self.acts.get(key, "")

    def sev(self, key: str) -> int:
        return classify(self.act(key), key)[3]

    def _key_of(self, key: str) -> str:
        return classify(self.act(key), key)[0]

    @property
    def is_done(self) -> bool:
        """✓ 済 = 対応済みで有効期間内。通知から丸ごと除外する。

        シート側の数式は「対応済み」に日付があれば、アラート条件に
        当てはまっていなくても ✓ 済 を出す。読めなかったときの保険として
        経過日数からも判定する (シートの数式と同じ条件)。
        """
        if any(self._key_of(k) == K_DONE for k in COLUMN_ORDER):
            return True
        return self.elapsed is not None and self.elapsed <= DONE_VALID_DAYS

    @property
    def is_overdue(self) -> bool:
        """⚠ 未着 = 対応したはずなのに在庫が戻っていない。最優先で通知する。

        シート側は「販売調整」列にしか ⚠ 未着 を出せない (梱包・発注の
        文言列を廃止したため)。梱包・発注だけが残っている商品も拾えるよう、
        経過日数 + まだ何か必要な状態か、でも判定する。
        """
        if any(self._key_of(k) == K_OVERDUE for k in COLUMN_ORDER):
            return True
        if self.elapsed is None or self.elapsed <= DONE_VALID_DAYS:
            return False
        return bool(self.act("sale")) or self.need_pack or self.need_order

    @property
    def elapsed(self) -> Optional[int]:
        """対応済みに入力された日からの経過日数。"""
        if not self.done_date:
            return None
        return (datetime.date.today() - self.done_date).days

    @property
    def hand(self) -> Optional[float]:
        """手元在庫 = 総在庫 - FBA - RSL - SC (荒瀬 + 事務所 + 中国 + 移動中)。"""
        if self.total is None:
            return None
        return self.total - (self.fba or 0) - (self.rsl or 0) - (self.sc or 0)

    @property
    def need_pack(self) -> bool:
        """📦 梱包が必要 = FBA梱包必要数 > 0。

        旧「梱包」列は MIN(手元在庫, FBA梱包必要数) = 手元にある分だけを
        出していて、隣の FBA梱包必要数 と数字が食い違い紛らわしかったため、
        必要総量そのものである FBA梱包必要数 をそのまま使う。
        """
        return isinstance(self.pack_need, (int, float)) and self.pack_need > 0

    @property
    def order_short(self) -> Optional[float]:
        """発注個数予測 − 発注中個数 (不足分)。足りていれば None。"""
        if not isinstance(self.order_qty, (int, float)):
            return None
        on = self.ordering if isinstance(self.ordering, (int, float)) else 0.0
        return self.order_qty - on if on < self.order_qty else None

    @property
    def need_order(self) -> bool:
        """🏭 発注が必要 = 発注中個数 < 発注個数予測。"""
        return self.order_short is not None

    @property
    def days_key(self) -> float:
        return (self.fba_days if isinstance(self.fba_days, (int, float))
                else 99999.0)

    @property
    def order_days_key(self) -> float:
        return (self.order_days if isinstance(self.order_days, (int, float))
                else -99999.0)   # ✖️ は最優先

    @property
    def order_days_disp(self) -> str:
        """総数発注残り日数の表示 (✖️ などの文字列もそのまま出す)。"""
        if isinstance(self.order_days, (int, float)):
            return f"{self.order_days:,.0f}日"
        return self.order_days_text or "-"


def num(v, unit: str = "") -> str:
    return f"{v:,.0f}{unit}" if isinstance(v, (int, float)) else "-"


def dstr(d: Optional[datetime.date]) -> str:
    return d.isoformat() if d else "-"


class Dashboard:
    """ダッシュボード２を通知セクションごとに分類したもの。

    分類の順番:
      1. ✓ 済 の行は最初に除外する (対応済み = 入庫待ち。通知しない)
      2. ⚠ 未着 の行は「最優先」セクションだけに載せる (他には出さない)
      3. 残りを 在庫切れ / 危険 / 梱包 / 販売調整 / 発注 / 要発注 に振り分ける

    梱包・発注はシートの文言列ではなく既存の数値列から判定する:
      梱包 = FBA梱包必要数 > 0
      発注 = 発注中個数 < 発注個数予測
    """

    __slots__ = ("rows", "url", "done", "overdue", "out_of_stock", "danger",
                 "order_urgent", "order_soon", "ordered_wait", "acts")

    def __init__(self, rows: List[DashRow], url: str, today: datetime.date):
        self.rows = rows
        self.url = url

        # --- 対応済み (✓ 済) は通知しない ---
        self.done = [r for r in rows if r.is_done]

        # --- ⚠ 未着 (対応済みなのに在庫が戻らない) は最優先 ---
        self.overdue = [r for r in rows if not r.is_done and r.is_overdue]
        self.overdue.sort(key=lambda r: (-(r.elapsed or 0), r.days_key, r.name))

        live = [r for r in rows if not r.is_done and not r.is_overdue]

        # --- 🛑 在庫切れ ---
        self.out_of_stock = [
            r for r in live
            if (r.fba is not None and r.fba <= 0)
            or (r.total is not None and r.total <= 0)
            or (r.fba_out_date is not None and r.fba_out_date <= today)
        ]
        self.out_of_stock.sort(key=lambda r: (r.days_key, r.name))
        gone = {r.name for r in self.out_of_stock}

        # --- ⚠️ 危険 (在庫僅少): FBA残り日数 1〜15日 ---
        self.danger = [
            r for r in live
            if r.name not in gone
            and isinstance(r.fba_days, (int, float))
            and 1 <= r.fba_days <= DAYS_DANGER
        ]
        self.danger.sort(key=lambda r: (r.days_key, r.name))

        # --- 📦 梱包 / 🔽 販売調整 / 🏭 発注 (担当別の「やること」) ---
        # 在庫切れ・危険と重複してもよい。あちらは「状況」、こちらは「誰が何を
        # するか」で、倉庫担当が自分の行だけを見られることを優先する。
        self.acts: Dict[str, List[DashRow]] = {}

        # 📦 梱包: FBA梱包必要数 (シートの数値列) が0より大きいもの。
        #          FBA残り日数が少ない順 = 送るのが急ぐ順に並べる。
        pack = [r for r in live if r.need_pack]
        pack.sort(key=lambda r: (r.days_key, r.name))
        self.acts["pack"] = pack

        # 🔽 販売調整: シート側「販売調整」列の文言から (全停止 → 減らす の順)
        sale = [r for r in live if r.act("sale")]
        sale.sort(key=lambda r: (r.sev("sale"), r.days_key, r.name))
        self.acts["sale"] = sale

        # 🏭 発注: 発注中個数が発注個数予測に足りていないもの。
        #          総数発注残り日数が少ない順 (✖️ が先頭)。
        order = [r for r in live if r.need_order]
        order.sort(key=lambda r: (r.order_days_key, r.name))
        self.acts["order"] = order

        ordered = {r.name for r in self.acts["order"]}

        # --- 📦 要発注 (リードタイム基準。上の「発注」に出た商品は除く) ---
        urgent, soon = [], []
        for r in live:
            if r.name in ordered:
                continue
            od, txt = r.order_days, r.order_days_text
            if "✖" in txt or (isinstance(od, (int, float)) and od < 0):
                urgent.append(r)
            elif isinstance(od, (int, float)) and 0 <= od <= DAYS_ORDER_SOON:
                soon.append(r)

        # 発注中が発注個数予測に足りている = もう手を打ってある → 様子見へ分離
        def ordered_enough(r: DashRow) -> bool:
            return (isinstance(r.ordering, (int, float)) and r.ordering > 0
                    and isinstance(r.order_qty, (int, float))
                    and r.ordering >= r.order_qty)

        self.ordered_wait = [r for r in urgent + soon if ordered_enough(r)]
        self.order_urgent = [r for r in urgent if not ordered_enough(r)]
        self.order_soon = [r for r in soon if not ordered_enough(r)]
        self.order_urgent.sort(key=lambda r: (r.order_days_key, r.name))
        self.order_soon.sort(key=lambda r: (r.order_days_key, r.name))
        self.ordered_wait.sort(key=lambda r: (r.order_days_key, r.name))

    @property
    def n_order(self) -> int:
        return len(self.order_urgent) + len(self.order_soon)

    @property
    def n_acts(self) -> int:
        return sum(len(self.acts[k]) for k in SECTION_ORDER)

    @property
    def total_items(self) -> int:
        """通知すべき在庫案件の総数 (0なら在庫アラートは送らない)。"""
        return (len(self.overdue) + len(self.out_of_stock) + len(self.danger)
                + self.n_acts + self.n_order + len(self.ordered_wait))


def scan_dashboard(sp, today: datetime.date) -> Tuple[Optional[Dashboard], List[str]]:
    """ダッシュボード２を読み、通知用に分類して返す (読み取り専用)。"""
    warnings: List[str] = []
    try:
        ws = sheets_retry(sp.worksheet, DASHBOARD_TAB)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"[{DASHBOARD_TAB}] シートを開けません: {e}")
        return None, warnings

    grid = sheets_retry(ws.get, f"A1:BZ{ws.row_count}",
                        value_render_option="UNFORMATTED_VALUE")
    if not grid:
        warnings.append(f"[{DASHBOARD_TAB}] 中身が読めません")
        return None, warnings

    header = [norm_header(h) for h in grid[0]]
    idx: Dict[str, Optional[int]] = {}
    wanted = [("order_days", DH_ORDER_DAYS), ("fba_days", DH_DAYS),
              ("done", DH_DONE), ("hint", DH_HINT),
              ("fba_out", DH_FBA_OUT_DATE), ("order_date", DH_ORDER_DATE),
              ("fba", DH_FBA_STOCK), ("rsl", DH_RSL_STOCK),
              ("sc", DH_SC_STOCK), ("order_qty", DH_ORDER_QTY),
              ("ordering", DH_ORDERING), ("pack_need", DH_PACK_NEED),
              ("lead", DH_LEAD_TIME)]
    wanted += [(key, DH_ACT[key]) for key in COLUMN_ORDER]
    for key, name in wanted:
        i = find_col(header, name)
        if i is None:
            warnings.append(f"[{DASHBOARD_TAB}] ヘッダー '{name}' が見つかりません")
        idx[key] = i

    if any(idx[key] is None for key in COLUMN_ORDER):
        extra = (f" (旧「{LEGACY_ALERT_HEADER}」列のままです)"
                 if find_col(header, LEGACY_ALERT_HEADER) is not None else "")
        warnings.append(
            f"[{DASHBOARD_TAB}] "
            f"{' / '.join(DH_ACT[k] for k in COLUMN_ORDER)} 列が無いため"
            f"集計できません{extra}。"
            f"dashboard_stock_alert_columns.py を実行してください")
        return None, warnings

    # 総在庫はヘッダーが空欄の列。総数発注予測日 と 平日販売数合計 の間にある。
    i_total = None
    i_wd = find_col(header, DH_WEEKDAY_SALES)
    if idx["order_date"] is not None and i_wd is not None:
        for i in range(idx["order_date"] + 1, i_wd):
            if not header[i]:
                i_total = i
                break
    if i_total is None:
        warnings.append(f"[{DASHBOARD_TAB}] 総在庫列 (ヘッダー空欄) を特定できません")
    idx["total"] = i_total

    def val(row: List, key: str):
        i = idx.get(key)
        if i is None or i >= len(row):
            return ""
        return row[i]

    rows: List[DashRow] = []
    for row in grid[1:]:
        if not row:
            continue
        name = str(row[0]).strip()
        if not name:
            continue
        rows.append(DashRow(
            name=name,
            order_days=to_number(val(row, "order_days")),
            order_days_text=str(val(row, "order_days")).strip(),
            fba_days=to_number(val(row, "fba_days")),
            acts={key: str(val(row, key)).strip() for key in COLUMN_ORDER},
            done_date=to_date(val(row, "done"), today),
            hint=str(val(row, "hint")).strip(),
            fba_out_date=to_date(val(row, "fba_out"), today),
            order_date=to_date(val(row, "order_date"), today),
            total=to_number(val(row, "total")),
            fba=to_number(val(row, "fba")),
            rsl=to_number(val(row, "rsl")),
            sc=to_number(val(row, "sc")),
            order_qty=to_number(val(row, "order_qty")),
            ordering=to_number(val(row, "ordering")),
            pack_need=to_number(val(row, "pack_need")),
            lead=to_number(val(row, "lead")),
        ))

    url = (f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
           f"/edit#gid={ws.id}")
    return Dashboard(rows, url, today), warnings


def format_stock_alerts(dash: Optional[Dashboard]) -> List[str]:
    """コンソールレポート用の在庫セクション。"""
    lines: List[str] = []
    lines.append("")
    lines.append("=" * 74)
    lines.append(" 在庫アラート (ダッシュボード２)")
    lines.append("=" * 74)
    if dash is None:
        lines.append("")
        lines.append("  ダッシュボード２を読めませんでした。")
        return lines
    if dash.total_items == 0:
        lines.append("")
        lines.append("  在庫アラートはありません。")
        return lines

    lines.append("")
    lines.append(f"  未着 {len(dash.overdue)}件 / "
                 f"在庫切れ {len(dash.out_of_stock)}件 / "
                 f"危険 {len(dash.danger)}件 / "
                 f"梱包 {len(dash.acts['pack'])}件 / "
                 f"販売調整 {len(dash.acts['sale'])}件 / "
                 f"発注 {len(dash.acts['order'])}件 / "
                 f"要発注 {dash.n_order}件 / "
                 f"様子見 {len(dash.ordered_wait)}件 "
                 f"(対応済みで除外 {len(dash.done)}件)")

    def sec(title: str, items: List[DashRow], fmt) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"{title} {len(items)}件")
        lines.append("-" * 74)
        for r in items:
            lines.extend(x for x in fmt(r) if x)

    sec(f"⚠ 未着 (対応済みなのに{DONE_VALID_DAYS}日経過)", dash.overdue, lambda r: [
        f"  {r.name:<20} 対応 {dstr(r.done_date)} から{r.elapsed}日 / "
        f"残{num(r.fba_days):>5}日 / FBA{num(r.fba):>7} / 手元{num(r.hand):>7}"])
    sec("🛑 在庫切れ", dash.out_of_stock, lambda r: [
        f"  {r.name:<20} FBA{num(r.fba):>8} / 総在庫{num(r.total):>8} / "
        f"手元{num(r.hand):>8} / 発注中{num(r.ordering):>8}"])
    sec("⚠️ 危険 (在庫僅少)", dash.danger, lambda r: [
        f"  {r.name:<20} 残{num(r.fba_days):>5}日 / FBA{num(r.fba):>7}",
        f"      {r.hint}" if r.hint else ""])
    # 📦 梱包: FBA梱包必要数 (シートAB列) が0より大きい商品
    sec(SECTION_TITLES["pack"], dash.acts["pack"], lambda r: [
        f"  {r.name:<20} 梱包必要{num(r.pack_need):>7} / "
        f"手元{num(r.hand):>7} / 残{num(r.fba_days):>5}日"])
    # 🔽 販売調整: シート側「販売調整」列の文言
    sec(SECTION_TITLES["sale"], dash.acts["sale"], lambda r: [
        f"  {r.name:<20} {r.act('sale'):<18} 残{num(r.fba_days):>5}日 / "
        f"FBA{num(r.fba):>7} / 手元{num(r.hand):>7}",
        f"      {r.hint}" if r.hint else ""])
    # 🏭 発注: 発注中個数 < 発注個数予測 の商品
    sec(SECTION_TITLES["order"], dash.acts["order"], lambda r: [
        f"  {r.name:<20} 発注予測{num(r.order_qty):>7} / "
        f"発注中{num(r.ordering):>7} / あと{r.order_days_disp:>7} / "
        f"LT{num(r.lead):>5}日"])
    sec("📦 要発注 (至急)", dash.order_urgent, lambda r: [
        f"  {r.name:<20} 発注予測日 {dstr(r.order_date):<12} "
        f"発注{num(r.order_qty):>8} / 発注中{num(r.ordering):>8} / "
        f"LT{num(r.lead):>5}日"])
    sec(f"📦 要発注 ({DAYS_ORDER_SOON}日以内)", dash.order_soon, lambda r: [
        f"  {r.name:<20} あと{num(r.order_days):>5}日 発注予測日 "
        f"{dstr(r.order_date):<12} 発注{num(r.order_qty):>8} / "
        f"発注中{num(r.ordering):>8} / LT{num(r.lead):>5}日"])
    sec("【発注済み・様子見】", dash.ordered_wait, lambda r: [
        f"  {r.name:<20} 発注中{num(r.ordering):>8} ≧ "
        f"予測{num(r.order_qty):>8} / 発注予測日 {dstr(r.order_date)}"])

    lines.append("")
    lines.append(f"  ※ 対応したら「{DH_DONE}」列に日付を入れてください。"
                 f"{DONE_VALID_DAYS}日間はこの通知から消えます")
    return lines


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
                  today: datetime.date, args,
                  dash: Optional[Dashboard] = None,
                  with_dash: bool = True) -> str:
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

    if with_dash:
        lines.extend(format_stock_alerts(dash))

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


def _section(out: List[str], title: str, items: List[DashRow], fmt) -> None:
    """ChatWork本文に1セクション書き出す (最大 MAX_PER_SECTION 件)。"""
    if not items:
        return
    out.append("")
    out.append(f"[b]{title}[/b] {len(items)}件")
    for r in items[:MAX_PER_SECTION]:
        out.extend(x for x in fmt(r) if x)
    if len(items) > MAX_PER_SECTION:
        out.append(f"  …他 {len(items) - MAX_PER_SECTION} 件")


def format_chatwork_stock(dash: Dashboard, today: datetime.date,
                          n_anomalies: int) -> str:
    """1通目: 在庫アラート。

    並び順:
      ⚠ 未着 (最優先) → 状況 (在庫切れ / 危険) → 担当別のやること
      (📦 梱包 / 🔽 販売調整 / 🏭 発注) → 要発注 → 発注済み・様子見
    「対応済み」に日付が入っている商品 (✓ 済) は最初から除いてある。
    """
    out = [f"[info][title]📊 在庫アラート ({today.isoformat()})[/title]"]
    head = (f"未着{len(dash.overdue)}件 / 在庫切れ{len(dash.out_of_stock)}件 / "
            f"危険{len(dash.danger)}件 / 梱包{len(dash.acts['pack'])}件 / "
            f"販売調整{len(dash.acts['sale'])}件 / 発注{len(dash.acts['order'])}件 / "
            f"データ異常{n_anomalies}件")
    if dash.done:
        head += f"\n（対応済みのため除外: {len(dash.done)}件）"
    out.append(head)

    _section(out, f"⚠ 未着（対応済みなのに{DONE_VALID_DAYS}日以上）", dash.overdue,
             lambda r: [
                 f"  ・{r.name} 対応{dstr(r.done_date)}から{r.elapsed}日 / "
                 f"残{num(r.fba_days)}日 / FBA{num(r.fba)} / 手元{num(r.hand)}"])

    _section(out, "🛑 在庫切れ", dash.out_of_stock, lambda r: [
        f"  ・{r.name} FBA{num(r.fba)} / 総在庫{num(r.total)} / "
        f"手元{num(r.hand)} / 発注中{num(r.ordering)}"])

    _section(out, "⚠️ 危険（在庫僅少）", dash.danger, lambda r: [
        f"  ・{r.name} 残{num(r.fba_days)}日 / FBA{num(r.fba)}",
        f"     {r.hint}" if r.hint else ""])

    # 担当別の「やること」。状況セクションと重複してよい (見る人が違う)
    # 梱包・発注はシートの「FBA梱包必要数」「発注個数予測 / 発注中個数」を
    # そのまま出す (シート側の数字と食い違わないようにするため)
    _section(out, SECTION_TITLES["pack"], dash.acts["pack"], lambda r: [
        f"  ・{r.name} 梱包{num(r.pack_need)}個（手元{num(r.hand)} / "
        f"残{num(r.fba_days)}日）"])
    _section(out, SECTION_TITLES["sale"], dash.acts["sale"], lambda r: [
        f"  ・{r.name} {r.act('sale')}（残{num(r.fba_days)}日 / "
        f"FBA{num(r.fba)} / 手元{num(r.hand)}）",
        f"     {r.hint}" if r.hint else ""])
    _section(out, SECTION_TITLES["order"], dash.acts["order"], lambda r: [
        f"  ・{r.name} 発注{num(r.order_qty)}個（発注中{num(r.ordering)} / "
        f"あと{r.order_days_disp} / LT{num(r.lead)}日）"])

    _section(out, "📦 要発注（至急・上記以外）", dash.order_urgent, lambda r: [
        f"  ・{r.name} 発注予測日{dstr(r.order_date)} / "
        f"発注{num(r.order_qty)}個 / 発注中{num(r.ordering)} / "
        f"LT{num(r.lead)}日"])

    _section(out, f"📦 要発注（{DAYS_ORDER_SOON}日以内・上記以外）", dash.order_soon,
             lambda r: [
                 f"  ・{r.name} あと{num(r.order_days)}日 ({dstr(r.order_date)}) / "
                 f"発注{num(r.order_qty)}個 / 発注中{num(r.ordering)} / "
                 f"LT{num(r.lead)}日"])

    _section(out, "【発注済み・様子見】", dash.ordered_wait, lambda r: [
        f"  ・{r.name} 発注中{num(r.ordering)} ≧ 予測{num(r.order_qty)} "
        f"({dstr(r.order_date)})"])

    out.append("")
    out.append(f"※対応したら「{DH_DONE}」列に日付を入れてください。"
               f"{DONE_VALID_DAYS}日間はこの通知から消えます")
    out.append(dash.url)
    out.append("[/info]")
    return "\n".join(out)


def format_chatwork_data(anomalies: List[Anomaly], today: datetime.date,
                         url: str) -> str:
    """2通目: 販売データ異常。"""
    by_kind: Dict[str, List[Anomaly]] = defaultdict(list)
    for a in anomalies:
        by_kind[a.kind].append(a)

    out = [f"[info][title]🔧 販売データ異常 ({today.isoformat()})[/title]"]
    out.append(" / ".join(f"{k} {len(by_kind[k])}件"
                          for k in KIND_ORDER if by_kind.get(k))
               or "異常なし")

    for k in KIND_ORDER:
        items = by_kind.get(k)
        if not items:
            continue
        out.append("")
        out.append(f"{KIND_ICON[k]} [b]{k}[/b] {len(items)}件")
        items.sort(key=lambda a: (a.sort_key, a.tab, a.code))
        for a in items[:MAX_PER_SECTION]:
            ch = f" {a.channel}" if a.channel and a.channel != "-" else ""
            out.append(f"  ・{a.code}{ch} {a.item}: {a.detail}")
        if len(items) > MAX_PER_SECTION:
            out.append(f"  …他 {len(items) - MAX_PER_SECTION} 件")

    out.append("")
    out.append(url)
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
                   help="ChatWork へ通知する (在庫アラート / 販売データ異常の2通)")
    p.add_argument("--only", choices=["stock", "data"],
                   help="通知を片方だけに絞る (stock=在庫アラート / data=販売データ異常)")
    p.add_argument("--print-body", action="store_true",
                   help="ChatWork へ送る本文を標準出力に表示する (送信はしない)")
    p.add_argument("--no-stock-alert", action="store_true",
                   help="ダッシュボード２の在庫アラートの集計をスキップする")
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

    # --- 在庫アラート (ダッシュボード２) ---
    dash: Optional[Dashboard] = None
    if not args.no_stock_alert:
        try:
            dash, sa_warns = scan_dashboard(sp, today)
            warnings.extend(sa_warns)
            if dash:
                print(f"  [{DASHBOARD_TAB}] 未着{len(dash.overdue)} / "
                      f"在庫切れ{len(dash.out_of_stock)} / "
                      f"危険{len(dash.danger)} / 梱包{len(dash.acts['pack'])} / "
                      f"販売調整{len(dash.acts['sale'])} / "
                      f"発注{len(dash.acts['order'])} / 要発注{dash.n_order} / "
                      f"様子見{len(dash.ordered_wait)} / "
                      f"対応済み除外{len(dash.done)}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"[{DASHBOARD_TAB}] 在庫アラートの集計に失敗: {e}")
            dash = None

    has_stock = bool(dash and dash.total_items)
    if not args.quiet or anomalies or has_stock:
        print(format_report(anomalies, warnings, today, args, dash,
                            with_dash=not args.no_stock_alert))

    # --- ChatWork 通知 (在庫アラート → 販売データ異常 の2通) ---
    if args.notify or args.print_body:
        url = dash.url if dash else (
            f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}/edit")
        # 送信先は種類ごとに分けられる。未設定なら共通のルームへ。
        msgs: List[Tuple[str, str, str]] = []   # (種類, ルームID, 本文)
        if args.only != "data" and has_stock:
            msgs.append(("在庫アラート",
                         settings.chatwork_room_id_stock or settings.chatwork_room_id,
                         format_chatwork_stock(dash, today, len(anomalies))))
        if args.only != "stock" and anomalies:
            msgs.append(("販売データ異常",
                         settings.chatwork_room_id_data or settings.chatwork_room_id,
                         format_chatwork_data(anomalies, today, url)))

        if args.print_body:
            for kind, _room, body in msgs:
                print(f"\n===== ChatWork本文: {kind} =====")
                print(body)
            if not msgs:
                print("\n(送信対象なし: 0件のため通知しません)")

        if args.notify and not args.print_body:
            token = settings.chatwork_api_token
            if not token:
                print("※ CHATWORK_API_TOKEN が無いため通知はスキップしました",
                      file=sys.stderr)
            elif not msgs:
                print("※ 通知対象が0件のため送信しませんでした", file=sys.stderr)
            for kind, room, body in (msgs if token else []):
                if not room:
                    print(f"※ {kind}: 送信先ルームが未設定のためスキップ",
                          file=sys.stderr)
                    continue
                try:
                    res = post_to_chatwork(token, room, body)
                    print(f"✓ ChatWork 通知 [{kind}] 送信しました "
                          f"(room={room} message_id={res.get('message_id')})",
                          file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    print(f"✗ ChatWork 通知 [{kind}] 失敗: {e}", file=sys.stderr)
                time.sleep(1.0)

    if failed_tabs:
        return 2
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main())
