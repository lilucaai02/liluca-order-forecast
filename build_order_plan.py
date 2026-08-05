#!/usr/bin/env python3
"""「発注計画」タブを作り、商品ごとの発注個数と、その根拠を書き出す。

■ なぜ作るか
ダッシュボード２の発注個数は「在庫切れ予測日から30日分」の固定で、
季節もイベントも出遅れも見ていない。光る首輪は秋に2.7倍売れるのに
夏の売れ行きで発注してしまう。そこでカレンダーを1日ずつ辿って積む。

    その日の需要 = 基準日販 × 季節係数(その月) × イベント係数(その日)
    発注個数     = カバー期間の需要合計 − 到着時点の在庫

■ それぞれの出どころ
  基準日販   : 直近30日の全体の販売実績 ÷ 30 (実測)
               前の30日と比べた増減も出す。最近伸びていれば発注も増える。
  季節係数   : タブごとに2025年の月別実績から算出 (7月=1.00)
               データが薄い月は 1.00 に倒す。
  イベント係数: 商品タブのアマゾンイベント係数行 (未来に登録済みのもの)
  到着時在庫 : ダッシュボードの在庫切れ予測日 − 到着日 を日数とみなし、
               同じくカレンダーで需要を積んで個数に直す
  次の発注日 : この発注が尽きる日 − リードタイム − 30日

■ 安全策
  - 既存タブは触らない。「発注計画」だけを作り直す。
  - 季節係数が出せない商品は 1.00 のまま計算し、その旨を根拠欄に書く。

使い方:
  python3 build_order_plan.py --dry-run
  python3 build_order_plan.py
  python3 build_order_plan.py --cover 60      # カバー日数を変える
"""

from __future__ import annotations

import argparse
import datetime
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings                    # noqa: E402
from oshima_tab_blocks_config import OSHIMA_TAB_BLOCKS  # noqa: E402
from src.fetch_safety import sheets_retry, set_default_socket_timeout  # noqa: E402

SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
DASH = "ダッシュボード２"
PLAN = "発注計画"
SERIAL_BASE = datetime.date(1899, 12, 30)

COVER_DAYS = 60       # 到着してから何日分を持たせるか
REORDER_LEAD = 30     # 在庫切れの何日前に発注するか (リードタイムに上乗せ)
SEASON_YEAR = 2025    # 季節係数を測る年
SEASON_BASE_MONTH = 7 # この月を 1.00 とする
MIN_DAYS_PER_MONTH = 20   # 季節係数を信用する最低日数(ブロック数×日数)

# 最小ロット。ここに無い商品は10個単位で切り上げるだけ。
MIN_LOT_RAW = {
    "tg-01": 300, "tg-02": 300, "gc-01": 300, "gc-02": 300,
}

LBL_SALES = "全体の販売実績"
LBL_EVENT = "アマゾンイベント"
LBL_COEF = "アマゾンイベント係数"

HEADERS = [
    "商品", "発注個数", "現行の予測", "最小ロット", "いま発注するか",
    "到着日", "カバー期間", "到着時の在庫", "基準日販", "前30日比", "直近7日",
    "季節係数", "イベント", "イベント上乗せ", "期間の需要",
    "在庫が尽きる日", "次の発注日", "根拠",
]


def norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).strip().lower()


def col_letter(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").replace("−", "-"))
    except (TypeError, ValueError):
        return None


def parse_date(v: Any) -> Optional[datetime.date]:
    m = re.match(r"^(?:(\d{4})/)?(\d{1,2})/(\d{1,2})$", str(v).strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)) if m.group(1) else datetime.date.today().year,
                             int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def min_lot(code: str) -> Optional[int]:
    c = norm(code)
    for k, v in MIN_LOT_RAW.items():
        if c.startswith(k):
            return v
    return None


class TabData:
    """1タブぶんの実績・イベント・季節係数。"""

    def __init__(self, sp, tab: str, today: datetime.date):
        ws = sheets_retry(sp.worksheet, tab)
        labels = sheets_retry(ws.col_values, 1)
        hdr = (sheets_retry(ws.get, "A1:ZZ1",
                            value_render_option="UNFORMATTED_VALUE") or [[]])[0]
        self.cols: Dict[datetime.date, int] = {}
        for j, v in enumerate(hdr, start=1):
            if isinstance(v, (int, float)) and v > 40000:
                self.cols[SERIAL_BASE + datetime.timedelta(days=int(v))] = j
        if not self.cols:
            self.ok = False
            return
        self.ok = True
        days = sorted(self.cols)
        c0, c1 = self.cols[days[0]], self.cols[days[-1]]
        blocks = OSHIMA_TAB_BLOCKS[tab]
        bounds = [b["asin_row"] for b in blocks] + [len(labels) + 2]

        ranges, meta = [], []
        for bi, b in enumerate(blocks):
            lo, hi = bounds[bi], bounds[bi + 1]
            rows = {}
            for x in range(lo, hi):
                lab = str(labels[x - 1]).strip() if x - 1 < len(labels) else ""
                if lab in (LBL_SALES, LBL_EVENT, LBL_COEF):
                    rows[lab] = x
            for lab in (LBL_SALES, LBL_EVENT, LBL_COEF):
                if lab in rows:
                    ranges.append(f"{col_letter(c0)}{rows[lab]}:{col_letter(c1)}{rows[lab]}")
                    meta.append((norm(b["code"]), lab))
        got = sheets_retry(ws.batch_get, ranges,
                           value_render_option="UNFORMATTED_VALUE") if ranges else []
        self.series: Dict[str, Dict[str, list]] = defaultdict(dict)
        for (code, lab), g in zip(meta, got):
            self.series[code][lab] = g[0] if g else []
        self.c0 = c0
        self.today = today
        self.index = self._season_index()

    def cell(self, code: str, lab: str, d: datetime.date) -> Any:
        arr = self.series.get(code, {}).get(lab, [])
        j = self.cols.get(d)
        if j is None:
            return None
        k = j - self.c0
        return arr[k] if k < len(arr) else None

    def _season_index(self) -> Dict[int, float]:
        """タブ全体の月別指数 (SEASON_BASE_MONTH = 1.00)。"""
        tot: Dict[int, float] = defaultdict(float)
        cnt: Dict[int, int] = defaultdict(int)
        for d in self.cols:
            if d.year != SEASON_YEAR or d >= self.today:
                continue
            for code in self.series:
                q = num(self.cell(code, LBL_SALES, d))
                if q is None:
                    continue
                tot[d.month] += q
                cnt[d.month] += 1
        avg = {m: tot[m] / cnt[m] for m in tot if cnt[m] >= MIN_DAYS_PER_MONTH}
        base = avg.get(SEASON_BASE_MONTH)
        if not base:
            return {}
        return {m: v / base for m, v in avg.items()}

    def daily(self, code: str, frm: int, to: int) -> float:
        """今日から frm〜to 日前の平均販売数。"""
        vals = []
        for i in range(frm, to + 1):
            q = num(self.cell(code, LBL_SALES, self.today - datetime.timedelta(days=i)))
            if q is not None:
                vals.append(q)
        return sum(vals) / len(vals) if vals else 0.0

    def season(self, month: int) -> float:
        return self.index.get(month, 1.0)

    def recent_season(self) -> float:
        """直近30日がどの時期にあたるか (季節係数の基準)。"""
        vals = [self.season((self.today - datetime.timedelta(days=i)).month)
                for i in range(1, 31)]
        return sum(vals) / len(vals) if vals else 1.0

    def coef(self, code: str, d: datetime.date) -> float:
        v = num(self.cell(code, LBL_COEF, d))
        return v if v and v > 0 else 1.0

    def event(self, code: str, d: datetime.date) -> str:
        v = self.cell(code, LBL_EVENT, d)
        return str(v).strip() if v else ""


def main() -> None:
    p = argparse.ArgumentParser(description="発注計画タブを作る")
    p.add_argument("--cover", type=int, default=COVER_DAYS,
                   help=f"到着後に持たせる日数 (既定 {COVER_DAYS})")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    set_default_socket_timeout()
    settings = Settings()
    today = datetime.date.today()

    import gspread
    from google.oauth2.service_account import Credentials
    gc = gspread.authorize(Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]))
    sp = sheets_retry(gc.open_by_key, SPREADSHEET_ID)

    dws = sheets_retry(sp.worksheet, DASH)
    dhdr = (sheets_retry(dws.get, "A1:BB1") or [[]])[0]

    def dcol(name: str) -> Optional[int]:
        """列見出しの1行目で照合する。

        「総数発注予測日(在庫切れ30日前-リードタイム)」のように、別の見出しの
        補足説明に語が含まれることがある。部分一致だとそちらを先に拾うので、
        まず1行目の完全一致で探し、無ければ部分一致に落とす。
        """
        for i, h in enumerate(dhdr, start=1):
            if str(h).split("\n")[0].strip() == name:
                return i
        for i, h in enumerate(dhdr, start=1):
            if name in str(h):
                return i
        return None

    ci = {k: dcol(k) for k in ("総数在庫切れ予測日", "総数発注予測日",
                               "発注個数予測", "リードタイム", "発注中個数")}
    drows = sheets_retry(dws.get, f"A2:{col_letter(len(dhdr))}62")
    dash: Dict[str, dict] = {}
    for r in drows:
        r = list(r) + [""] * len(dhdr)
        if not str(r[0]).strip():
            continue
        dash[norm(r[0])] = {
            "name": str(r[0]).strip(),
            **{k: (r[v - 1] if v else "") for k, v in ci.items()},
        }

    tabs: Dict[str, TabData] = {}
    code_tab: Dict[str, str] = {}
    for tab, blocks in OSHIMA_TAB_BLOCKS.items():
        td = TabData(sp, tab, today)
        if not td.ok:
            continue
        tabs[tab] = td
        for b in blocks:
            code_tab[norm(b["code"])] = tab
        idx = td.index
        print(f"[{tab}] 季節係数 " +
              (" ".join(f"{m}月{idx[m]:.2f}" for m in sorted(idx)) if idx else "(データ不足→1.00)"))

    ALIAS = {"mp-02mhd4": "mp-02mhd", "pci-01gray": "pci-01gray1",
             "pg-01m": "pg-01ml", "pg-01l": "pg-01xl",
             "wb-01s": "s", "wb-01m": "m", "wb-01l": "l", "wb-01xl": "xl",
             "ts-01": "ts-01mw"}

    out: List[list] = []
    for key, d in dash.items():
        code = key if key in code_tab else ALIAS.get(key, key)
        tab = code_tab.get(code)
        if not tab:
            continue
        td = tabs[tab]
        lt = num(d["リードタイム"])
        so = parse_date(d["総数在庫切れ予測日"])
        od = parse_date(d["総数発注予測日"])
        cur = num(d["発注個数予測"])
        if lt is None or so is None:
            continue
        b30, prev30, b7 = td.daily(code, 1, 30), td.daily(code, 31, 60), td.daily(code, 1, 7)
        if b30 <= 0:
            continue
        rs = td.recent_season() or 1.0
        arrive = today + datetime.timedelta(days=int(lt))

        def need_on(day: datetime.date) -> float:
            return b30 * td.season(day.month) / rs * td.coef(code, day)

        # カバー期間の需要
        total = 0.0
        ev_add = 0.0
        evd: Dict[str, int] = defaultdict(int)
        for i in range(args.cover):
            dd = arrive + datetime.timedelta(days=i)
            base = b30 * td.season(dd.month) / rs
            c = td.coef(code, dd)
            total += base * c
            name = td.event(code, dd)
            if name:
                evd[name] += 1
                ev_add += base * (c - 1)
        # 到着時点で残っている在庫を個数に直す
        have = sum(need_on(arrive + datetime.timedelta(days=i))
                   for i in range(max(0, (so - arrive).days)))
        need = max(0.0, total - have)
        lot = min_lot(code)
        q = int(math.ceil(need / 10) * 10)
        if lot and 0 < q < lot:
            q = lot

        # 発注分がいつ尽きるか → 次の発注日
        left = q + max(0.0, have)
        i = 0
        while left > 0 and i < 1500:
            left -= need_on(arrive + datetime.timedelta(days=i))
            i += 1
        end = arrive + datetime.timedelta(days=i)
        nxt = end - datetime.timedelta(days=int(lt) + REORDER_LEAD)

        months = sorted({(arrive + datetime.timedelta(days=i)).month
                         for i in range(args.cover)})
        season_txt = " / ".join(f"{m}月 {td.season(m):.2f}" for m in months)
        if not td.index:
            season_txt = "データ不足のため1.00"
        ev_txt = " / ".join(f"{k} {v}日" for k, v in evd.items()) or "なし"
        trend = f"{(b30 - prev30) / prev30 * 100:+.0f}%" if prev30 > 0 else "—"
        gap = (so - arrive).days

        why = []
        if gap < 0:
            why.append(f"到着時に{-gap}日欠品するため満量")
        if td.index and max(td.season(m) for m in months) >= 1.3:
            why.append("需要期に入るため季節係数で増加")
        if prev30 > 0 and b30 / prev30 >= 1.2:
            why.append(f"直近の売れ行きが前月比{trend}")
        elif prev30 > 0 and b30 / prev30 <= 0.8:
            why.append(f"直近の売れ行きが前月比{trend}")
        if ev_add > total * 0.15:
            why.append(f"期間中のイベントで{ev_add:.0f}個上乗せ")
        if lot and need < lot:
            why.append(f"必要{need:.0f}個だが最小ロット{lot}個")
        if q == 0:
            why.append("到着時の在庫で足りるため発注不要")

        out.append([
            d["name"], q, cur if cur is not None else "", lot or "",
            "発注する" if (od and od <= today) else (f"{od}まで待つ" if od else ""),
            arrive.strftime("%Y/%m/%d"),
            f"{arrive.strftime('%m/%d')}〜{(arrive + datetime.timedelta(days=args.cover - 1)).strftime('%m/%d')}",
            f"{gap}日分", round(b30, 1), trend, round(b7, 1),
            season_txt, ev_txt, round(ev_add), round(total),
            end.strftime("%Y/%m/%d"), nxt.strftime("%Y/%m/%d"),
            "／".join(why) or "通常",
        ])

    out.sort(key=lambda r: -(r[1] or 0))
    print(f"\n対象 {len(out)}商品 / 発注合計 {sum(r[1] for r in out):,}個")
    if args.dry_run:
        for r in out[:12]:
            print(f"  {r[0]:20s} {r[1]:>6,} (現行 {r[2]}) 次の発注 {r[16]}  {r[17]}")
        print("\n[dry-run] シートには書き込みませんでした")
        return

    try:
        ws = sheets_retry(sp.worksheet, PLAN)
        sheets_retry(ws.clear)
    except Exception:                                    # noqa: BLE001
        ws = sheets_retry(sp.add_worksheet, title=PLAN,
                          rows=len(out) + 10, cols=len(HEADERS) + 2)

    stamp = [[f"発注計画  作成日 {today}  "
              f"（到着してから{args.cover}日分を持たせる前提／"
              f"次の発注日＝在庫が尽きる日−リードタイム−{REORDER_LEAD}日）"]]
    sheets_retry(ws.update, range_name="A1", values=stamp,
                 value_input_option="USER_ENTERED")
    sheets_retry(ws.update, range_name=f"A2:{col_letter(len(HEADERS))}2",
                 values=[HEADERS], value_input_option="USER_ENTERED")
    if out:
        sheets_retry(ws.update, range_name=f"A3:{col_letter(len(HEADERS))}{len(out) + 2}",
                     values=out, value_input_option="USER_ENTERED")

    sheets_retry(sp.batch_update, {"requests": [
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": .19, "green": .35, "blue": .55},
                "textFormat": {"bold": True, "foregroundColor":
                               {"red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP",
                "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat"}},
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 12}}},
            "fields": "userEnteredFormat.textFormat"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": ws.id,
                           "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": 1}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 2,
                      "endRowIndex": len(out) + 2, "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 1, "green": .95, "blue": .8},
                "horizontalAlignment": "RIGHT"}},
            "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)"}},
        {"autoResizeDimensions": {"dimensions": {
            "sheetId": ws.id, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": len(HEADERS)}}},
    ]})
    print(f"\n→ 「{PLAN}」タブに {len(out)}行 書き込みました")
    print(f"   https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


if __name__ == "__main__":
    main()
