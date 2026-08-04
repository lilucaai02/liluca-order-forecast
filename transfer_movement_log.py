#!/usr/bin/env python3
"""
「発注と在庫移動一覧」タブ → 商品別(在庫)タブの「在庫変更（数量）/FROM/TO」への転記。

対象スプレッドシート: 発注予測コピー(大島用)
  1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU

--- 一覧タブの列 (A〜S) ---------------------------------------------------
  A 移動日(日付シリアル) / B 商品SKU / C 移動元 / D 移動先 / E 個数
  F 追跡番号 / G 梱包完成予定日 / H 到着予定日 / I 状態 / J 備考欄
  K データ更新(チェックボックス) = 出発側の転記済みフラグ
  L データ日付   ← L22 の ARRAYFORMULA が自動記入。絶対に直接書かない
  M 到着個数/発送個数 (数式)
  N 今到着個数 / O 最後到着日付
  P データ更新(チェックボックス) = 到着側の転記済みフラグ
  Q データ日付   ← Q22 の ARRAYFORMULA が自動記入。絶対に直接書かない
  R 納品内容 =EXACT(E,N) / S 運送時間 =DATEDIF(A,O,"d")

--- 転記ルール -------------------------------------------------------------
  A. 輸送を伴う移動 (移動元あり・中国工場以外)
       K未チェック                     → A列の日に  E個 / FROM=map(移動元) / TO=輸送中
       P未チェック かつ N・O が埋まる  → O列の日に  (N−転記済み)個 / FROM=輸送中 / TO=map(移動先)
  B. 発注 (移動元・移動先とも空欄 / 状態=発注中)
       K未チェック                     → A列の日に  E個 / FROM=(空)       / TO=発注中
       P未チェック かつ N・O が埋まる  → O列の日に  (N−転記済み)個 / FROM=発注中 / TO=map(移動先)
  B2. 工場入荷 (移動元=中国工場 → 移動先=イーウーパスポート　中国)
       生産完了を意味する。発注中から中国倉庫へ移すだけで輸送段階は挟まない。
       K未チェック                     → A列の日に  E個 / FROM=発注中     / TO=中国
       ※1本の記録で完結するため P (到着) 側の処理は行わない。
  B3. 数量調整 (移動元か移動先の片方だけ + 状態=到着)
       ずれ修正・不良品・棚卸差異など。輸送を伴わず1本で完結する。
       移動元が空欄 → A列の日に E個 / FROM=(空) / TO=map(移動先)   その拠点に足す
       移動先が空欄 → A列の日に E個 / FROM=map(移動元) / TO=(空)   その拠点から引く
       備考欄(J列)の内容はセルメモに転記する。
  C. 日付衝突: 当日→翌日→前日→翌々日→前々日… の順に空き列を探す (±14日)
  D. メモ: 数量・FROM・TO の3セルすべてに同一メモを付ける
  E. 到着日の自動推定はしない。N列(今到着個数)・O列(最後到着日付) が
     人手で埋まっている行だけを到着として扱う。N・O には書き込まない。

--- 一覧への書き戻し -------------------------------------------------------
  出発／発注を転記 → K列 = TRUE のみ (L列は書かない)
  到着を転記       → N列のセルメモに「転記済み到着個数: n」を残す
                     全量到着 (N>=E) のときだけ P列 = TRUE / I列 = 到着
                     ※分納の途中で P を立てると残りが転記されなくなるため。
                       N列は累計なので、次回はメモとの差分だけを転記する。

--- 安全機構 ---------------------------------------------------------------
  1. ABSOLUTE_MIN_ROW (=952) より前の行は絶対に処理しない (コードに固定)
  2. 同一商品の±7日窓に「同じ数量・同じFROM/TO」があればスキップ
  3. 書き込み先3セルが空であることを確認してから書く (上書き禁止)
  4. ロックファイルによる多重起動ガード
  5. --dry-run で書き込まず内容だけ表示
  6. --row N で特定の1行だけ処理
  7. --skip-row N / --exclude-sku CODE で個別に除外 (手入力済みの行など)
  8. Sheets API は src/fetch_safety.py の sheets_retry でリトライ

使い方:
  python3 transfer_movement_log.py --row 952 --dry-run
  python3 transfer_movement_log.py --row 952
  python3 transfer_movement_log.py --dry-run            # 952行目以降を全件
  python3 transfer_movement_log.py --skip-row 987       # 987行目だけ除外
  python3 transfer_movement_log.py --exclude-sku gc-02rainbow-35
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import os
import sys
import tempfile
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings                    # noqa: E402
from oshima_tab_blocks_config import OSHIMA_TAB_BLOCKS  # noqa: E402
from src.fetch_safety import sheets_retry, set_default_socket_timeout  # noqa: E402

SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
LIST_SHEET = "発注と在庫移動一覧"
SERIAL_BASE = datetime.date(1899, 12, 30)

# ---- 安全装置: この行より前は何があっても触らない -------------------------
# 951行目以前は人手で転記済み。過去分を再転記すると在庫が二重計上される。
ABSOLUTE_MIN_ROW = 952

DATE_SHIFT_LIMIT = 14   # 日付衝突時にずらす上限(日)
DUP_WINDOW_DAYS = 7     # 重複チェックの前後日数

# 分納の追跡用。N列(今到着個数)は累計なので、そのまま転記すると前回転記した
# 分まで二重に入る。どこまで転記したかを N列のセルメモに残し、差分だけ書く。
NOTE_PREFIX = "転記済み到着個数: "

LOCK_PATH = os.path.join(tempfile.gettempdir(), "transfer_movement_log.lock")

# 商品タブの FROM/TO セルは strict なプルダウン。ここにない値は書けない。
ALLOWED_LOCATIONS = {
    "FBA", "中国", "RSL", "Stock Crew",
    "荒瀬(Amazon)", "荒瀬(他)", "事務所(Amazon)", "事務所(他)",
    "輸送中", "発注中",
}

# 商品タブに対応する在庫行が無く、在庫計算に反映できない拠点。
# (プルダウンにも無いため書き込むとデータ検証エラーになる)
UNSUPPORTED_LOCATIONS = {"就労支援所", "アールステージ"}

# 「中国工場」が移動元のときは特別扱い。生産が終わって中国倉庫へ入るという
# 意味なので、商品タブでは「発注中 → 中国」の1本で記録する (輸送段階なし)。
FACTORY_SOURCE = "中国工場"
FACTORY_DEST = "中国"

# 一覧の状態(I列)。数量調整の行はこの状態で書いてもらう。
STATE_ARRIVED = "到着"

# 一覧の移動元/移動先 → 商品タブの FROM/TO 値。キーは norm() 済み。
LOCATION_MAP_RAW = {
    "イーウーパスポート　中国": "中国",
    "中国工場": "中国",      # 移動先のとき。移動元のときは FACTORY_SOURCE 側で処理
    "事務所 amazon用": "事務所(Amazon)",
    "事務所 amazon以外": "事務所(他)",
    "荒瀬倉庫 amazon用": "荒瀬(Amazon)",
    "荒瀬倉庫 amazon以外": "荒瀬(他)",
    "FBA": "FBA",
    "RSL": "RSL",
    "Stock Crew": "Stock Crew",
    "武智商店": "発注中",
    "就労支援所": "就労支援所",
    "アールステージ": "アールステージ",
}

# 一覧のSKU → 商品タブのブロックコード。キー・値とも norm() 済みで比較する。
# ※ PG-01 は名前が1つずつズレている (PG-01m→pg-01ml / PG-01l→pg-01xl)。
#    ユーザーが一覧のプルダウンを新表記に直しても動くよう、新表記は
#    別名を経由せず直接ヒットする (別名表に新表記を入れていないため)。
SKU_ALIAS_RAW = {
    "MP-02MHD4": "MP-02MHD",
    "PCI-01gray": "PCI-01gray1",
    "PG-01m": "pg-01ml",
    "PG-01l": "pg-01xl",
    "WB-01s": "S",
    "WB-01l": "L",
    "TS-01": "ts-01mw",
}

LABEL_QTY = "在庫変更（数量）"
LABEL_FROM = "FROM"
LABEL_TO = "TO"


# --- 小道具 -----------------------------------------------------------------
def norm(s: Any) -> str:
    """NFKC正規化 + 前後空白除去 + 小文字化。

    全角スペース (U+3000) は NFKC で半角スペースになるため、
    'イーウーパスポート　中国' のような表記ゆれも吸収できる。
    """
    if s is None:
        return ""
    return unicodedata.normalize("NFKC", str(s)).strip().lower()


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def serial_to_date(v: Any) -> Optional[datetime.date]:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return SERIAL_BASE + datetime.timedelta(days=n)


def fmt_date(d: datetime.date) -> str:
    return f"{d.year}/{d.month}/{d.day}"


def is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


LOCATION_MAP = {norm(k): v for k, v in LOCATION_MAP_RAW.items()}
SKU_ALIAS = {norm(k): norm(v) for k, v in SKU_ALIAS_RAW.items()}
NORM_FACTORY = norm(FACTORY_SOURCE)


def build_block_index() -> Dict[str, Tuple[str, dict]]:
    """norm(商品コード) → (タブ名, ブロック定義)。"""
    index: Dict[str, Tuple[str, dict]] = {}
    for tab, blocks in OSHIMA_TAB_BLOCKS.items():
        for blk in blocks:
            key = norm(blk["code"])
            if key in index:
                raise RuntimeError(
                    f"商品コードが重複: {blk['code']!r} "
                    f"({index[key][0]} と {tab})")
            index[key] = (tab, blk)
    return index


def resolve_block(sku: str, index: Dict[str, Tuple[str, dict]]
                  ) -> Optional[Tuple[str, dict]]:
    key = norm(sku)
    key = SKU_ALIAS.get(key, key)      # 別名を先に解決 (旧PG-01表記の誤爆防止)
    return index.get(key)


def map_location(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """(商品タブの値, エラー理由) を返す。空欄は ('', None)。"""
    if is_blank(raw):
        return "", None
    key = norm(raw)
    if key not in LOCATION_MAP:
        return None, f"未知の拠点名 {raw!r} (対応表に無い)"
    val = LOCATION_MAP[key]
    if val in UNSUPPORTED_LOCATIONS:
        return None, (f"拠点 {val!r} は商品タブに対応する在庫行が無いため"
                      f"在庫計算に反映できません (プルダウンにも存在しない)")
    if val not in ALLOWED_LOCATIONS:
        return None, f"{val!r} は商品タブのプルダウンに存在しません"
    return val, None


# --- 転記タスク -------------------------------------------------------------
class Task:
    """商品タブに書き込む1レコード分。"""

    def __init__(self, list_row: int, kind: str, sku: str, tab: str, blk: dict,
                 want_date: datetime.date, qty: int,
                 cell_from: str, cell_to: str,
                 journey_from: str, journey_to: str, extra_note: str = ""):
        self.list_row = list_row
        # "departure" / "order" / "factory_in" / "arrival"
        self.kind = kind
        self.sku = sku
        self.tab = tab
        self.blk = blk
        self.want_date = want_date
        self.qty = qty
        self.cell_from = cell_from      # 商品タブ FROM 行に書く値
        self.cell_to = cell_to          # 商品タブ TO 行に書く値
        self.journey_from = journey_from  # メモ用「どこから」
        self.journey_to = journey_to      # メモ用「どこへ」
        self.extra_note = extra_note
        self.actual_date: Optional[datetime.date] = None
        self.col_idx: Optional[int] = None
        self.skip_reason: Optional[str] = None

    def note_text(self) -> str:
        head = f"{self.journey_from} → {self.journey_to} ／ {self.qty}個 ／ 一覧{self.list_row}行目"
        if self.kind == "departure":
            body = f"（輸送中の記録。到着後に {self.journey_to} へ計上されます）"
        elif self.kind == "order":
            body = f"（発注中の記録。到着後に {self.journey_to} へ計上されます）"
        elif self.kind == "factory_in":
            body = "（工場からの入荷）"
        elif self.kind == "adjust":
            body = "（数量のずれを直す記録。移動ではありません）"
        else:
            body = f"（輸送中から {self.journey_to} への到着記録）"
        lines = [head, body]
        if self.extra_note:
            lines.append(self.extra_note)
        if self.actual_date and self.actual_date != self.want_date:
            lines.append(
                f"本来は{fmt_date(self.want_date)}の記録"
                f"（同日に他の移動があったため{fmt_date(self.actual_date)}に記録）")
        return "\n".join(lines)


def parse_transferred(note: Any) -> int:
    """N列のセルメモから、これまでに転記した到着個数を読む。"""
    s = str(note or "")
    i = s.find(NOTE_PREFIX)
    if i < 0:
        return 0
    digits = ""
    for ch in s[i + len(NOTE_PREFIX):]:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else 0


def build_tasks(list_row: int, row: List[Any],
                index: Dict[str, Tuple[str, dict]],
                warn: List[str],
                exclude_skus: Optional[set] = None,
                transferred: int = 0) -> List[Task]:
    """一覧の1行から転記タスクを組み立てる。"""
    exclude_skus = exclude_skus or set()
    def cell(i: int) -> Any:
        return row[i - 1] if len(row) >= i else ""

    move_date = serial_to_date(cell(1))
    sku = cell(2)
    src_raw, dst_raw = cell(3), cell(4)
    qty_raw = cell(5)
    state = cell(9)
    flag_k, flag_p = cell(11), cell(16)
    arrive_qty_raw, arrive_date_raw = cell(14), cell(15)

    if is_blank(sku):
        return []
    if norm(sku) in exclude_skus:
        warn.append(f"{list_row}行目: SKU {sku!r} は --exclude-sku 指定のため除外")
        return []

    resolved = resolve_block(sku, index)
    if resolved is None:
        warn.append(f"{list_row}行目: SKU {sku!r} に対応する商品タブのブロックが見つかりません")
        return []
    tab, blk = resolved

    src, src_err = map_location(src_raw)
    dst, dst_err = map_location(dst_raw)

    tasks: List[Task] = []
    is_order = is_blank(src_raw) and is_blank(dst_raw)
    # 「中国工場」発 = 生産完了。発注中 → 中国 の1本で記録し輸送段階は挟まない。
    is_factory = norm(src_raw) == NORM_FACTORY
    # ずれ修正・不良品などの数量調整。輸送を伴わないので1本の記録で完結する。
    #   移動元が空欄        → その拠点に足す (検品で多かった等)
    #   移動先が空欄        → その拠点から引く (不良品・棚卸差異等)
    #   移動先が「中国工場」 → 工場へ返す = その拠点から引く
    #     移動元が「中国工場」を工場入荷として扱うのと対称。現場が
    #     「工場に戻した」と書きたくなるので、空欄と同じ意味で受け付ける。
    is_return_to_factory = (norm(state) == norm(STATE_ARRIVED)
                            and norm(dst_raw) == NORM_FACTORY
                            and not is_blank(src_raw)
                            and norm(src_raw) != NORM_FACTORY)
    is_adjust = (norm(state) == norm(STATE_ARRIVED)
                 and is_blank(src_raw) != is_blank(dst_raw)) or is_return_to_factory
    if is_return_to_factory:
        dst, dst_err = "", None       # 行き先なし = 在庫から消える扱い

    # --- 出発 / 発注 / 工場入荷 -------------------------------------------
    if not bool(flag_k):
        if move_date is None:
            warn.append(f"{list_row}行目: 移動日(A列)が読めないため出発分をスキップ")
        else:
            try:
                qty = int(qty_raw)
            except (TypeError, ValueError):
                qty = None
            if not qty:
                warn.append(f"{list_row}行目: 個数(E列)が数値でないため出発分をスキップ ({qty_raw!r})")
            elif is_adjust:
                # 数量調整。片方が空欄のまま FROM/TO を書くと、その分だけ
                # 在庫が増減し、どこにも移動しない。
                if src_err or dst_err:
                    warn.append(f"{list_row}行目: 調整の拠点が転記できません — "
                                f"{src_err or dst_err}")
                else:
                    place = dst or src
                    sign = "＋" if dst else "−"
                    memo = str(cell(10) or "").strip()      # J列 備考欄
                    note = f"（数量調整: {place} を {sign}{qty}個）"
                    if memo:
                        note = f"（数量調整: {place} を {sign}{qty}個 ／ {memo}）"
                    tasks.append(Task(list_row, "adjust", sku, tab, blk,
                                      move_date, qty, src or "", dst or "",
                                      src or "(調整)", dst or "(調整)", note))
            elif is_order:
                # 移動元・移動先とも空欄 (状態=発注中) → FROM=空欄 / TO=発注中
                if dst_err:
                    warn.append(f"{list_row}行目: 移動先が転記できません — {dst_err}")
                elif dst:
                    warn.append(f"{list_row}行目: 移動元が空欄なのに移動先 {dst_raw!r} が"
                                f"入っています。発注として扱えないためスキップ")
                elif norm(state) not in ("", norm("発注中")):
                    warn.append(f"{list_row}行目: 移動元・移動先が空欄ですが状態が"
                                f"{state!r} です。発注として扱えないためスキップ")
                else:
                    tasks.append(Task(list_row, "order", sku, tab, blk,
                                      move_date, qty, "", "発注中",
                                      "発注", "(未定)"))
            elif is_factory:
                # 工場 → 中国倉庫。発注中から中国へ移すだけ。
                if dst_err:
                    warn.append(f"{list_row}行目: 移動先が転記できません — {dst_err}")
                elif dst != FACTORY_DEST:
                    warn.append(
                        f"{list_row}行目: 移動元が「{FACTORY_SOURCE}」ですが移動先が "
                        f"{dst_raw!r} ({dst or '空欄'}) です。"
                        f"「{FACTORY_SOURCE}」→「{FACTORY_DEST}」以外は"
                        f"扱いが決まっていないためスキップします")
                else:
                    tasks.append(Task(list_row, "factory_in", sku, tab, blk,
                                      move_date, qty, "発注中", FACTORY_DEST,
                                      "発注中", FACTORY_DEST))
            elif src_err:
                warn.append(f"{list_row}行目: 移動元が転記できません — {src_err}")
            elif dst_err:
                warn.append(f"{list_row}行目: 移動先が転記できません — {dst_err}")
            else:
                tasks.append(Task(list_row, "departure", sku, tab, blk,
                                  move_date, qty, src, "輸送中",
                                  src, dst or "(未定)"))

    # --- 到着 (N列・O列が人手で埋まっている行だけ。自動推定はしない) ------
    if is_adjust:
        if not is_blank(arrive_qty_raw) or not is_blank(arrive_date_raw):
            warn.append(f"{list_row}行目: 数量調整の行は1本で完結するため、"
                        f"N/O列があっても到着分は転記しません")
    elif is_factory:
        if not is_blank(arrive_qty_raw) or not is_blank(arrive_date_raw):
            warn.append(f"{list_row}行目: 移動元が「{FACTORY_SOURCE}」の行は"
                        f"輸送段階が無いため、N/O列があっても到着分は転記しません")
    elif not bool(flag_p) and not is_blank(arrive_qty_raw) and not is_blank(arrive_date_raw):
        arrive_date = serial_to_date(arrive_date_raw)
        try:
            arrive_qty = int(arrive_qty_raw)
        except (TypeError, ValueError):
            arrive_qty = None
        if arrive_date is None:
            warn.append(f"{list_row}行目: 最後到着日付(O列)が読めないため到着分をスキップ")
        elif not arrive_qty:
            warn.append(f"{list_row}行目: 今到着個数(N列)が数値でないため到着分をスキップ ({arrive_qty_raw!r})")
        elif dst_err:
            warn.append(f"{list_row}行目: 移動先が転記できません — {dst_err}")
        elif not dst:
            warn.append(f"{list_row}行目: 移動先(D列)が空欄のため到着分をスキップ")
        elif arrive_qty - transferred <= 0:
            pass    # 前回までに転記済み。増えていないので何もしない
        else:
            # 発注行は「発注中」から、輸送行は「輸送中」から出す
            from_val = "発注中" if is_order else "輸送中"
            # N列は累計。前回転記した分を引いて、増えた分だけを書く。
            delta = arrive_qty - transferred
            extra = ""
            try:
                ship = int(qty_raw)
                if transferred:
                    extra = (f"（分納: 発送{ship}個のうち今回{delta}個が到着"
                             f"／到着累計{arrive_qty}個）")
                elif ship > arrive_qty:
                    extra = f"（分納: 発送{ship}個のうち{arrive_qty}個が到着）"
            except (TypeError, ValueError):
                pass
            tasks.append(Task(list_row, "arrival", sku, tab, blk,
                              arrive_date, delta, from_val, dst,
                              from_val, dst, extra))
    return tasks


# --- タブ側のキャッシュ -----------------------------------------------------
class TabView:
    """商品タブ1枚分の読み取り結果 (日付列・ブロック3行の既存値)。"""

    def __init__(self, ws):
        self.ws = ws
        self.sheet_id = ws.id
        self.title = ws.title
        last_col = col_letter(ws.col_count)
        got = sheets_retry(self.ws.batch_get,
                           [f"A1:{last_col}1", f"A1:A{ws.row_count}"],
                           value_render_option="UNFORMATTED_VALUE")
        row1 = got[0][0] if got and got[0] else []
        self.serial_to_col: Dict[int, int] = {}
        for i, v in enumerate(row1, start=1):
            if isinstance(v, (int, float)) and int(v) > 0:
                self.serial_to_col.setdefault(int(v), i)
        # A列ラベル (ブロックの行構造を必ずラベルで検証するために使う)
        self._labels: List[str] = [
            (r[0] if r else "") for r in (got[1] if len(got) > 1 else [])]
        self._rows: Dict[int, List[Any]] = {}
        self._last_col = last_col

    def load_block(self, blk: dict) -> None:
        need = [blk["change_qty_row"], blk["from_row"], blk["to_row"]]
        need = [r for r in need if r not in self._rows]
        if not need:
            return
        ranges = [f"A{r}:{self._last_col}{r}" for r in need]
        got = sheets_retry(self.ws.batch_get, ranges,
                           value_render_option="UNFORMATTED_VALUE")
        for r, g in zip(need, got):
            self._rows[r] = g[0] if g else []

    def cell(self, row: int, col_idx: int) -> Any:
        vals = self._rows.get(row, [])
        return vals[col_idx - 1] if len(vals) >= col_idx else ""

    def reserve(self, blk: dict, col_idx: int, task: "Task") -> None:
        """同一実行内で同じ列を2度使わないよう、キャッシュ上を埋める。"""
        for row, val in ((blk["change_qty_row"], task.qty),
                         (blk["from_row"], task.cell_from),
                         (blk["to_row"], task.cell_to)):
            vals = self._rows.setdefault(row, [])
            if len(vals) < col_idx:
                vals.extend([""] * (col_idx - len(vals)))
            vals[col_idx - 1] = val if val != "" else "(予約)"

    def label(self, row: int) -> str:
        return str(self._labels[row - 1]) if len(self._labels) >= row else ""

    def col_for_date(self, d: datetime.date) -> Optional[int]:
        return self.serial_to_col.get((d - SERIAL_BASE).days)


def verify_block_labels(view: TabView, blk: dict
                        ) -> Tuple[Optional[str], Optional[str]]:
    """A列ラベルでブロックの3行を検証する。

    戻り値は (致命的エラー, 警告)。
    3行のラベルが違えば行構造がズレているので致命的エラー。
    商品コード行の不一致は警告に留める (TG-02のようにタブのA列が
    旧コードのまま運用されているブロックがあるため)。
    """
    checks = [
        (blk["change_qty_row"], LABEL_QTY),
        (blk["from_row"], LABEL_FROM),
        (blk["to_row"], LABEL_TO),
    ]
    for row, want in checks:
        got = view.label(row)
        if norm(got) != norm(want):
            return (f"{view.title} {row}行目のA列ラベルが {got!r} で、"
                    f"期待する {want!r} と一致しません", None)
    warning = None
    code_row = blk.get("code_row")
    if code_row and norm(view.label(code_row)) != norm(blk["code"]):
        warning = (f"{view.title} A{code_row} の商品コードは "
                   f"{view.label(code_row)!r} で、設定の {blk['code']!r} と"
                   f"表記が異なります (行位置はラベルで検証済み)")
    return None, warning


def is_occupied(view: TabView, blk: dict, col_idx: int) -> bool:
    for row in (blk["change_qty_row"], blk["from_row"], blk["to_row"]):
        if not is_blank(view.cell(row, col_idx)):
            return True
    return False


def find_free_column(view: TabView, blk: dict, want: datetime.date
                     ) -> Tuple[Optional[int], Optional[datetime.date]]:
    """当日→翌日→前日→翌々日→前々日… の順に空き列を探す。"""
    offsets = [0]
    for i in range(1, DATE_SHIFT_LIMIT + 1):
        offsets += [i, -i]
    for off in offsets:
        d = want + datetime.timedelta(days=off)
        col = view.col_for_date(d)
        if col is None:
            continue
        if not is_occupied(view, blk, col):
            return col, d
    return None, None


def find_duplicate(view: TabView, blk: dict, task: Task
                   ) -> Optional[Tuple[int, datetime.date]]:
    """±7日窓に同じ数量・同じFROM/TOの記録があればその列を返す。"""
    for off in range(-DUP_WINDOW_DAYS, DUP_WINDOW_DAYS + 1):
        d = task.want_date + datetime.timedelta(days=off)
        col = view.col_for_date(d)
        if col is None:
            continue
        q = view.cell(blk["change_qty_row"], col)
        f = view.cell(blk["from_row"], col)
        t = view.cell(blk["to_row"], col)
        if is_blank(q):
            continue
        try:
            same_qty = int(q) == task.qty
        except (TypeError, ValueError):
            same_qty = False
        if same_qty and norm(f) == norm(task.cell_from) and norm(t) == norm(task.cell_to):
            return col, d
    return None


# --- 書き込みリクエスト -----------------------------------------------------
def cell_range(sheet_id: int, row: int, col_idx: int) -> dict:
    return {"sheetId": sheet_id,
            "startRowIndex": row - 1, "endRowIndex": row,
            "startColumnIndex": col_idx - 1, "endColumnIndex": col_idx}


def update_cell_request(sheet_id: int, row: int, col_idx: int,
                        value: Any, note: Optional[str]) -> dict:
    """1セルに値とメモを同時に書くリクエスト。value が None ならメモのみ。"""
    cell: Dict[str, Any] = {}
    fields = []
    if value is not None:
        if isinstance(value, bool):
            cell["userEnteredValue"] = {"boolValue": value}
        elif isinstance(value, (int, float)):
            cell["userEnteredValue"] = {"numberValue": value}
        else:
            cell["userEnteredValue"] = {"stringValue": str(value)}
        fields.append("userEnteredValue")
    if note is not None:
        cell["note"] = note
        fields.append("note")
    return {"updateCells": {
        "range": cell_range(sheet_id, row, col_idx),
        "rows": [{"values": [cell]}],
        "fields": ",".join(fields)}}


def task_requests(view: TabView, task: Task) -> List[dict]:
    blk, sid, col = task.blk, view.sheet_id, task.col_idx
    note = task.note_text()
    reqs = [update_cell_request(sid, blk["change_qty_row"], col, task.qty, note)]
    # FROM が空欄 (発注) のときは値を書かずメモだけ付ける
    reqs.append(update_cell_request(
        sid, blk["from_row"], col,
        task.cell_from if task.cell_from else None, note))
    reqs.append(update_cell_request(sid, blk["to_row"], col, task.cell_to, note))
    return reqs


# --- メイン -----------------------------------------------------------------
def acquire_lock():
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"エラー: 既に実行中です (ロック {LOCK_PATH})", file=sys.stderr)
        sys.exit(1)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main() -> None:
    p = argparse.ArgumentParser(description="在庫移動一覧 → 商品タブ 転記")
    p.add_argument("--row", type=int, help="この行だけ処理する")
    p.add_argument("--from-row", type=int, default=ABSOLUTE_MIN_ROW,
                   help=f"処理開始行 (既定 {ABSOLUTE_MIN_ROW}。これ未満は拒否)")
    p.add_argument("--to-row", type=int, help="処理終了行 (既定: 最終データ行)")
    p.add_argument("--dry-run", action="store_true", help="書き込まず内容だけ表示")
    p.add_argument("--skip-row", type=int, action="append", default=[],
                   metavar="N",
                   help="この一覧行を処理しない (K列にもチェックを入れない)。複数指定可")
    p.add_argument("--exclude-sku", action="append", default=[], metavar="CODE",
                   help="このSKUの行を処理しない。複数指定可")
    p.add_argument("--force-row", type=int, action="append", default=[],
                   metavar="N",
                   help="この行は重複チェックを飛ばして転記する。同じ数量・同じ"
                        "経路の別便を、続けて発送したときに使う")
    p.add_argument("--arrival-row", type=int, action="append", default=[],
                   metavar="N",
                   help=f"{ABSOLUTE_MIN_ROW}行目より前のこの行を、到着分だけ"
                        f"例外的に転記する。出発分が1つでも出る行は中止する。複数指定可")
    args = p.parse_args()

    skip_rows = set(args.skip_row)
    exclude_skus = {norm(s) for s in args.exclude_sku}
    arrival_rows = set(args.arrival_row)
    force_rows = set(args.force_row)

    if args.row is not None:
        start_row = end_row = args.row
    else:
        start_row, end_row = args.from_row, args.to_row

    # 安全装置1: 過去行の再転記を絶対に許さない
    if start_row < ABSOLUTE_MIN_ROW:
        print(f"エラー: {ABSOLUTE_MIN_ROW}行目より前は処理できません "
              f"(指定 {start_row})。951行目以前は転記済みのため、"
              f"再転記すると在庫が二重計上されます。", file=sys.stderr)
        sys.exit(1)
    if end_row is not None and end_row < start_row:
        print("エラー: --to-row が --from-row より小さいです", file=sys.stderr)
        sys.exit(1)

    set_default_socket_timeout()
    lock = acquire_lock()  # noqa: F841 - プロセス終了まで保持する

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env の認証情報設定を確認してください", file=sys.stderr)
        sys.exit(1)

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sp = sheets_retry(gc.open_by_key, SPREADSHEET_ID)
    list_ws = sheets_retry(sp.worksheet, LIST_SHEET)

    if end_row is None:
        end_row = list_ws.row_count
    print(f"=== 在庫移動一覧 転記 ({start_row}〜{end_row}行目) "
          f"{'[dry-run]' if args.dry_run else ''} ===", file=sys.stderr)

    rows = sheets_retry(list_ws.get, f"A{start_row}:S{end_row}",
                        value_render_option="UNFORMATTED_VALUE")

    # N列のセルメモ (= これまでに転記した到着個数)。分納で二重に書かないため。
    note_lo = min([start_row] + list(arrival_rows))
    notes_raw = sheets_retry(list_ws.get_notes,
                             grid_range=f"N{note_lo}:N{end_row}") or []
    transferred: Dict[int, int] = {}
    for k, nrow in enumerate(notes_raw):
        v = parse_transferred(nrow[0] if nrow else "")
        if v:
            transferred[note_lo + k] = v

    index = build_block_index()
    warn: List[str] = []
    tasks: List[Task] = []
    for i, row in enumerate(rows):
        list_row = start_row + i
        if list_row < ABSOLUTE_MIN_ROW:      # 安全装置1 (二重の歯止め)
            continue
        if list_row in skip_rows:
            warn.append(f"{list_row}行目: --skip-row 指定のため処理しません "
                        f"(K列にもチェックを入れません)")
            continue
        tasks += build_tasks(list_row, list(row), index, warn, exclude_skus,
                             transferred.get(list_row, 0))

    # --- 例外: 951行目以前の「到着だけ」を転記する ------------------------
    # 出発分は人手で転記済み (K列=TRUE) なので、到着を入れないと商品タブの
    # 「輸送中」が永久に減らない。出発分が出る行は二重計上になるため中止する。
    if arrival_rows:
        lo, hi = min(arrival_rows), max(arrival_rows)
        extra = sheets_retry(list_ws.get, f"A{lo}:S{hi}",
                             value_render_option="UNFORMATTED_VALUE")
        for n in sorted(arrival_rows):
            if n >= ABSOLUTE_MIN_ROW:
                print(f"エラー: --arrival-row {n} は {ABSOLUTE_MIN_ROW}行目より前を"
                      f"指定してください (通常の範囲で処理されます)", file=sys.stderr)
                sys.exit(1)
            raw = extra[n - lo] if n - lo < len(extra) else []
            ts = build_tasks(n, list(raw), index, warn, exclude_skus,
                             transferred.get(n, 0))
            bad = [t for t in ts if t.kind != "arrival"]
            if bad:
                print(f"エラー: {n}行目は到着以外の転記({bad[0].kind})が発生するため"
                      f"中止します。K列(データ更新)にチェックが入っているか"
                      f"確認してください。", file=sys.stderr)
                sys.exit(1)
            if not ts:
                warn.append(f"{n}行目: 転記できる到着分がありません "
                            f"(N列・O列が未記入か、P列が既にTRUE)")
            tasks += ts

    if not tasks:
        for w in warn:
            print(f"  [警告] {w}", file=sys.stderr)
        print("転記対象はありません", file=sys.stderr)
        return

    # タブ単位で読み込み → 列の決定
    views: Dict[str, TabView] = {}
    for t in tasks:
        if t.tab not in views:
            views[t.tab] = TabView(sheets_retry(sp.worksheet, t.tab))
        view = views[t.tab]
        view.load_block(t.blk)

    checked: set = set()
    for t in tasks:
        view = views[t.tab]
        key = (t.tab, t.blk["code"])
        if key not in checked:
            err, note = verify_block_labels(view, t.blk)
            if err:
                print(f"エラー: {err}", file=sys.stderr)
                sys.exit(1)
            if note:
                warn.append(note)
            checked.add(key)

        dup = None if t.list_row in force_rows else find_duplicate(view, t.blk, t)
        if dup:
            t.skip_reason = (f"重複の可能性: {fmt_date(dup[1])} "
                             f"({col_letter(dup[0])}列) に同じ "
                             f"{t.qty}個 / {t.cell_from or '(空)'}→{t.cell_to} "
                             f"の記録があります")
            continue
        col, actual = find_free_column(view, t.blk, t.want_date)
        if col is None:
            t.skip_reason = (f"{fmt_date(t.want_date)} の前後{DATE_SHIFT_LIMIT}日に"
                             f"空き列がありません")
            continue
        t.col_idx, t.actual_date = col, actual
        view.reserve(t.blk, col, t)

    # --- 表示 -------------------------------------------------------------
    kind_ja = {"departure": "出発", "order": "発注",
               "factory_in": "工場入荷", "arrival": "到着",
               "adjust": "数量調整"}
    todo = [t for t in tasks if t.skip_reason is None]
    print(f"\n--- 転記内容 {len(todo)}件 / スキップ {len(tasks) - len(todo)}件 ---",
          file=sys.stderr)
    for t in tasks:
        head = f"一覧{t.list_row}行目 [{kind_ja[t.kind]}] {t.sku} → {t.tab}"
        if t.skip_reason:
            print(f"  × {head}: スキップ — {t.skip_reason}", file=sys.stderr)
            continue
        c = col_letter(t.col_idx)
        print(f"  ○ {head}", file=sys.stderr)
        print(f"      {c}{t.blk['change_qty_row']} = {t.qty}", file=sys.stderr)
        print(f"      {c}{t.blk['from_row']} = {t.cell_from or '(空欄のまま)'}", file=sys.stderr)
        print(f"      {c}{t.blk['to_row']} = {t.cell_to}", file=sys.stderr)
        print(f"      日付: {fmt_date(t.actual_date)}"
              + ("" if t.actual_date == t.want_date
                 else f" (本来 {fmt_date(t.want_date)} からずらし)"), file=sys.stderr)
        for line in t.note_text().split("\n"):
            print(f"      メモ| {line}", file=sys.stderr)
    for w in warn:
        print(f"  [警告] {w}", file=sys.stderr)

    if not todo:
        print("\n書き込む対象がありません", file=sys.stderr)
        return
    if args.dry_run:
        print("\n[dry-run] 書き込みは行いませんでした", file=sys.stderr)
        return

    # --- 書き込み前の最終確認 (上書き禁止) --------------------------------
    for t in todo:
        view = views[t.tab]
        fresh = sheets_retry(
            view.ws.batch_get,
            [f"{col_letter(t.col_idx)}{r}" for r in
             (t.blk["change_qty_row"], t.blk["from_row"], t.blk["to_row"])],
            value_render_option="UNFORMATTED_VALUE")
        vals = [(g[0][0] if g and g[0] else "") for g in fresh]
        if any(not is_blank(v) for v in vals):
            print(f"エラー: 書き込み先が空ではありません "
                  f"({t.tab} {col_letter(t.col_idx)}列 = {vals})。中止します。",
                  file=sys.stderr)
            sys.exit(1)

    # --- 商品タブへ書き込み (値+メモを1リクエストで) ----------------------
    for tab, view in views.items():
        reqs: List[dict] = []
        for t in todo:
            if t.tab == tab:
                reqs += task_requests(view, t)
        if reqs:
            sheets_retry(sp.batch_update, {"requests": reqs})
            print(f"\n→ {tab}: {len(reqs)}セル書き込み完了", file=sys.stderr)

    # --- 一覧への書き戻し (K/P のみ。L・Q は ARRAYFORMULA なので触らない) --
    list_reqs: List[dict] = []
    note_reqs: List[Tuple[int, str]] = []
    done_msgs: List[str] = []
    by_row: Dict[int, List[Task]] = {}
    for t in todo:
        by_row.setdefault(t.list_row, []).append(t)
    for list_row, ts in sorted(by_row.items()):
        row_vals = rows[list_row - start_row] if len(rows) > list_row - start_row else []

        def cell(i: int) -> Any:
            return row_vals[i - 1] if len(row_vals) >= i else ""

        if any(t.kind in ("departure", "order", "factory_in", "adjust") for t in ts):
            list_reqs.append(update_cell_request(list_ws.id, list_row, 11, True, None))
            done_msgs.append(f"K{list_row} = TRUE")
        if any(t.kind == "arrival" for t in ts):
            # どこまで転記したかを N列のメモに残す。分納の残りが後日届いたとき、
            # 差分だけを転記するために使う。
            arrived = 0
            try:
                arrived = int(cell(14))
            except (TypeError, ValueError):
                pass
            note_reqs.append((list_row, f"{NOTE_PREFIX}{arrived}"))
            full = False
            try:
                full = int(cell(14)) >= int(cell(5))
            except (TypeError, ValueError):
                pass
            # P(到着の転記済みフラグ)は全量届いたときだけ立てる。
            # 分納の途中で立てると、残りが届いても転記されなくなる。
            if full:
                list_reqs.append(
                    update_cell_request(list_ws.id, list_row, 16, True, None))
                done_msgs.append(f"P{list_row} = TRUE")
                list_reqs.append(
                    update_cell_request(list_ws.id, list_row, 9, "到着", None))
                done_msgs.append(f"I{list_row} = 到着")
            else:
                done_msgs.append(f"N{list_row}メモ = 転記済み{arrived}個 (分納継続)")
    if list_reqs:
        sheets_retry(sp.batch_update, {"requests": list_reqs})
    if note_reqs:
        sheets_retry(list_ws.update_notes,
                     {f"N{r}": text for r, text in note_reqs})
    if done_msgs:
        print(f"→ 一覧の書き戻し: {', '.join(done_msgs)}", file=sys.stderr)

    print(f"\n完了 → https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


if __name__ == "__main__":
    main()
