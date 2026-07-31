#!/usr/bin/env python3
"""ダッシュボード２に「販売調整」「対応済み」「調整の目安」を作る。

運用ルール (ユーザーの前提):
  在庫が30日分になった時点で補充が到着している想定で回している。
  つまり「FBA残り日数が30日を切っている」= 予定どおりなら入庫しているはずの
  時点で入庫していない = 供給が間に合っていない、というサイン。
  1ヶ月の猶予があればセールを調整して在庫を延ばせるので、
  30日を切った時点でアラートを出して判断材料にする。

--- 経緯 -----------------------------------------------------------------
2026-07-31 (1): 「在庫警告」1列に梱包・セール調整・発注が混在していて
  担当者が自分の行を探せなかったため、担当別に3列 (梱包/販売調整/発注) へ分割。
2026-07-31 (2 / 現行): その「梱包」「発注」列を削除した。
  梱包 = MIN(手元在庫, FBA梱包必要数)、発注 = 発注個数予測 − 発注中 と
  「必要総量とは違う数」を出していたため、隣の AB「FBA梱包必要数」・
  Z「発注個数予測」と数字が食い違って紛らわしい、という指摘による。
  梱包・発注は既存の数値列を見れば分かるので文言列は持たず、
  ChatWork通知だけを既存列 (AB / Z・AA) から組み立てる
  (check_sales_anomaly.py)。シートに残すのは次の3列:
    販売調整 (G) … 送っても足りないので売る量を絞る、という判断だけ
    対応済み (H) … ユーザーが対応日を手入力する欄 (数式なし)
    調整の目安(I) … 従来どおり

判定 (在庫日数 = FBA残り日数 C列 / 手元在庫 = 総在庫 - FBA - RSL - SC):
  前提: 在庫日数が数値でない or 30より大きい → 空欄

  [販売調整]  手元在庫 < FBA梱包必要数 のときだけ (= 梱包しても足りない)
    在庫日数 <= 15                       → 🛑 セール全停止    (赤)
    それ以外                             → 🔽 セール減らす    (橙)

  [対応済み] に日付が入っていたら、上の判定より優先して:
    経過 <= DONE_VALID_DAYS 日           → ✓ 済 M/d           (灰)
    それを過ぎてもまだアラート条件のまま → ⚠ 未着 ◯日経過     (赤)
    (アラート条件から外れていれば空欄。= 入庫して解決した)
  これで「梱包・発送して入庫見込みがあるのにアラートが出続ける」を止める。

  C列は "✖️" や "−" の文字列になることがあるため ISNUMBER で弾く。
  全体を IFERROR でラップし、エラー時は空欄。
  文言・色・日数の定数は stock_alert_labels.py に集約している。

--- 調整の目安 ---
「販売調整」が空欄でない商品にのみ表示:
  「このまま◯日 ／ 停止で◯日 ／ 14日持たせるには◯個/日」

  このまま◯日 = FBA残り日数 (C列) …… 何もしなければ在庫が尽きるまでの日数
  停止で◯日   = FBA在庫数 ÷ 抑制ペース (切り捨て)
    抑制ペース = 該当商品タブの
                 「直近7平日セール以外平均」と「直近セール以外加重平均」の
                 大きい方 (安全側 = 消費が多い前提で見積もる)
    抑制ペースが0以下なら「停止で在庫維持可」
  14日持たせるには◯個/日 = FBA在庫数 ÷ 14 (小数第1位)
    目標日数は stock_alert_labels.HINT_TARGET_DAYS の1箇所で定義。

参照行は商品タブのA列ラベルで特定する (config の行番号は当てにしない)。

使い方:
  python3 dashboard_stock_alert_columns.py --dry-run
  python3 dashboard_stock_alert_columns.py            # 未移行なら列挿入から
  python3 dashboard_stock_alert_columns.py --formulas-only  # 移行済みの再設定用
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from config.settings import Settings
from fetch_safety import sheets_retry
from stock_alert_labels import (
    CF_COLORS,
    COLUMN_ORDER,
    COLUMN_SPECS,
    DAYS_ALERT,
    DAYS_URGENT,
    DONE_DATE_FORMAT,
    DONE_HEADER_NOTE,
    DONE_INPUT_BG,
    DONE_VALID_DAYS,
    HINT_TARGET_DAYS,
    H_DONE,
    H_HINT,
    LEGACY_ALERT_HEADER,
    LEGACY_DROPPED_HEADERS,
    LEGACY_MATCH_TEXTS,
    M_DONE,
    M_OVERDUE,
    NEW_HEADERS,
)

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
DASH = "ダッシュボード２"

# 管理する列 (販売調整 / 対応済み)。調整の目安はこの右に残る。
N_NEW = len(NEW_HEADERS)          # = 2
N_INSERT = N_NEW - 1              # 旧「在庫警告」列を1列目に流用するので1列だけ挿入

# 判定・表示に使う既存ヘッダー (実際の列位置はヘッダー文字列から解決する)
H_DAYS = "FBA残り日数"
H_FBA_DATE = "FBA在庫切れ予測日"      # 参照タブ/行の解決に使う
H_TOTAL_STOCK = ""                    # 総在庫 (ヘッダーが空欄の列)
H_FBA = "FBA在庫数"
H_RSL = "RSL在庫数"
H_SC = "ストッククルー在庫数"
H_PACK_NEED = "FBA梱包必要数"

# 商品タブ側のA列ラベル
L_STOCK_FORECAST = "在庫予想"          # FBA在庫切れ予測日が参照している行
L_AMAZON_SALES = "amazonFBA販売実績"
L_WEEKDAY_AVG = "直近7平日セール以外平均"
L_WEIGHTED_AVG = "直近セール以外加重平均"

REF_RE = re.compile(r"'([^']+)'!\$(\d+):\$(\d+)")

# --- 書式 ------------------------------------------------------------------
# ヘッダー: 直前の「残り日数」グループ (C〜F) と同じ濃紺 + 白太字
HEADER_BG = {"red": 0.043137256, "green": 0.3254902, "blue": 0.5803922}
WHITE = {"red": 1, "green": 1, "blue": 1}
BLACK = {"red": 0, "green": 0, "blue": 0}
# データ行は白地。隣接列の背景を引き継がせず、条件付き書式の色を素直に見せる
DATA_BG = WHITE

COL_WIDTH_ACT = 140    # 梱包 / 販売調整 / 発注
COL_WIDTH_DONE = 80    # 対応済み
COL_WIDTH_HINT = 250   # 調整の目安


def a1col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def first_data_ref(formula: str):
    """行1(日付行)以外の最初の 'タブ'!$N:$N 参照を返す。"""
    if not formula or not formula.startswith("="):
        return None, None
    tab = None
    for m in REF_RE.finditer(formula):
        if tab is None:
            tab = m.group(1)
        if int(m.group(2)) != 1:
            return m.group(1), int(m.group(2))
    return tab, None


def cf_ranges(sp, sheet_id: int):
    """対象シートの条件付き書式ルール一覧 (index順) を返す。"""
    meta = sheets_retry(
        sp.fetch_sheet_metadata,
        params={"fields": "sheets(properties(sheetId,title),conditionalFormats)"})
    for x in meta["sheets"]:
        if x["properties"]["sheetId"] == sheet_id:
            return x.get("conditionalFormats", [])
    return []


CELL_REF_RE = re.compile(r"(\$?)([A-Z]{1,3})(\$?)(\d+)")


def shift_relative_cols(formula: str, delta: int) -> str:
    """条件付き書式の CUSTOM_FORMULA 内の相対列参照を delta 列ずらす。

    CF の数式は「範囲の左上セル」を基準にした相対参照として解釈される。
    範囲の開始列を動かしたら、$ の付いていない列参照も同じだけ動かさないと
    別の列を指してしまう (過去に G2:G62 → I2:I62 と戻したとき、自分自身を
    指すはずの G2 が2列左 = 新設した列を指してしまう事故があった)。
    """
    if not delta:
        return formula

    def rep(m):
        dollar_col, col, dollar_row, row = m.groups()
        if dollar_col:                       # $G のような絶対列は動かさない
            return m.group(0)
        n = 0
        for ch in col:
            n = n * 26 + (ord(ch) - 64)
        n += delta
        if n < 1:
            return m.group(0)
        return f"{dollar_col}{a1col(n)}{dollar_row}{row}"

    return CELL_REF_RE.sub(rep, formula)


def retarget_rule(rule: dict, delta: int) -> dict:
    """CUSTOM_FORMULA を持つルールの相対列参照を delta 列ずらしたコピーを返す。"""
    out = json.loads(json.dumps(rule))
    br = out.get("booleanRule", {})
    cond = br.get("condition", {})
    if cond.get("type") != "CUSTOM_FORMULA":
        return out
    for v in cond.get("values", []):
        f = v.get("userEnteredValue")
        if isinstance(f, str) and f.startswith("="):
            v["userEnteredValue"] = shift_relative_cols(f, delta)
    return out


def expected_after_insert(rng: dict, insert_at: int, count: int) -> dict:
    """列挿入後にあるべき範囲を計算する。

    Sheets は挿入位置がちょうど範囲の先頭にあるとき範囲を「広げて」しまう
    ことがある (過去にL列の赤字ルールが挿入列へ波及した事例あり)。
    本来は次の規則に従うべきなので、これを期待値として突き合わせる。
      s >= insert_at    → 右へずらす
      e <= insert_at    → そのまま
      s < insert_at < e → 本当にまたいでいるので広げる
    """
    s = rng.get("startColumnIndex")
    e = rng.get("endColumnIndex")
    out = dict(rng)
    if s is None or e is None:
        return out
    if s >= insert_at:
        out["startColumnIndex"] = s + count
        out["endColumnIndex"] = e + count
    elif e <= insert_at:
        pass
    else:
        out["endColumnIndex"] = e + count
    return out


def resolve_avg_rows(labels: list[str], base: int) -> tuple[int | None, int | None]:
    """在庫予想行(base)が属するブロックの Amazon チャネル平均行を返す。

    ブロック内では「直近7平日セール以外平均」「直近セール以外加重平均」が
    Amazon / 楽天 / Yahoo の3回ずつ現れる。Amazon の販売実績行より前にある
    最後の出現が Amazon の分。
    """
    def label(r: int) -> str:
        return labels[r - 1].strip() if 0 < r <= len(labels) else ""

    # 直前ブロックの「在庫予想」より後ろだけを見る (ブロック境界)
    lo = 1
    for r in range(base - 1, 0, -1):
        if label(r) == L_STOCK_FORECAST:
            lo = r + 1
            break

    sales = None
    for r in range(lo, base):
        if label(r) == L_AMAZON_SALES:
            sales = r
    if sales is None:
        return None, None

    wk7 = wavg = None
    for r in range(lo, sales):
        if label(r) == L_WEEKDAY_AVG:
            wk7 = r
        elif label(r) == L_WEIGHTED_AVG:
            wavg = r
    return wk7, wavg


# ---------------------------------------------------------------------------
# 数式
# ---------------------------------------------------------------------------

def refs(cols: dict, r: int) -> dict:
    """行 r の参照式をまとめて作る。"""
    days = f"${cols['days']}{r}"
    hand = (f"(N(${cols['total']}{r})-N(${cols['fba']}{r})"
            f"-N(${cols['rsl']}{r})-N(${cols['sc']}{r}))")
    return {
        "days": days,
        "hand": hand,
        "need": f"N(${cols['need']}{r})",
        "done": f"${cols['done']}{r}",
    }


def base_sale(x: dict) -> str:
    """「販売調整」の素の判定。梱包しても足りないときだけ出す。"""
    return (
        f'IF({x["hand"]}<{x["need"]},'
        f'IF({x["days"]}<={DAYS_URGENT},"🛑 セール全停止","🔽 セール減らす"),"")')


BASE_BUILDERS = {"sale": base_sale}


def build_alert_formula(col_key: str, cols: dict, r: int) -> str:
    """アラート列 (現行は「販売調整」のみ) の数式。

    構造:
      IFERROR(LET(base, <素の判定>,
        対応済みに日付あり → 済 / 未着 (base が残っているときだけ未着)
        なければ base), "")
    """
    x = refs(cols, r)
    gate = (f'IF(NOT(ISNUMBER({x["days"]})),"",'
            f'IF({x["days"]}>{DAYS_ALERT},"",'
            f'{BASE_BUILDERS[col_key](x)}))')
    done = x["done"]
    elapsed = f'(TODAY()-{done})'
    return (
        f'=IFERROR(LET(base,{gate},'
        f'IF(ISNUMBER({done}),'
        f'IF({elapsed}<={DONE_VALID_DAYS},'
        f'"✓ {M_DONE} "&TEXT({done},"{DONE_DATE_FORMAT}"),'
        f'IF(base="","","⚠ {M_OVERDUE} "&TEXT({elapsed},"0")&"日経過")),'
        f'base)),"")')


def build_hint_formula(cols: dict, r: int, tab: str, wk7: int, wavg: int) -> str:
    """調整の目安の数式。

    「このまま◯日 ／ 停止で◯日 ／ 14日持たせるには◯個/日」
      このまま = FBA残り日数 (C列) をそのまま
      停止で   = FBA在庫数 ÷ 抑制ペース (切り捨て)
      14日     = HINT_TARGET_DAYS
    アラート列 (現行は「販売調整」のみ) が空欄の行には出さない。
    セールをどれだけ絞るかの判断材料なので、参照するのは販売調整列だけ。
    """
    t = f"'{tab}'"

    def at(row: int) -> str:
        return f"N(INDEX({t}!${row}:${row},1,MATCH(TODAY(),{t}!$1:$1,0)))"

    pace = f"MAX({at(wk7)},{at(wavg)})"
    fba = f"N(${cols['fba']}{r})"
    days = f"${cols['days']}{r}"
    conds = [f'${cols[k]}{r}=""' for k in COLUMN_ORDER]
    empty = conds[0] if len(conds) == 1 else "AND(" + ",".join(conds) + ")"
    return (
        f'=IFERROR(IF({empty},"",'
        f'"このまま"&TEXT(ROUND({days},0),"0")&"日 ／ "&'
        f'IF({pace}<=0,"停止で在庫維持可",'
        f'"停止で"&ROUNDDOWN({fba}/{pace},0)&"日")'
        f'&" ／ {HINT_TARGET_DAYS}日持たせるには"'
        f'&TEXT(ROUND({fba}/{HINT_TARGET_DAYS},1),"0.0")&"個/日"),"")')


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--formulas-only", action="store_true",
                    help="列挿入をスキップし、数式と書式のみ再設定する")
    args = ap.parse_args()

    import gspread
    from google.oauth2.service_account import Credentials

    settings = Settings()
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = sheets_retry(gc.open_by_key, DEST_ID)
    ws = sheets_retry(sp.worksheet, DASH)
    sheet_id = ws.id

    forms = sheets_retry(
        sp.values_get, f"'{DASH}'!A1:BZ{ws.row_count}",
        params={"valueRenderOption": "FORMULA"}).get("values", [])
    header = forms[0]
    print("=== 現在のヘッダー ===")
    for i, h in enumerate(header):
        print(f"  {a1col(i+1)}: {h!r}")

    migrated = NEW_HEADERS[0] in header
    if migrated:
        anchor = header.index(NEW_HEADERS[0])
        missing = [h for h in NEW_HEADERS if h not in header]
        if missing:
            print(f"移行が中途半端です (欠けている列: {missing})。"
                  f"手で戻してから再実行してください", file=sys.stderr)
            return 1
        args.formulas_only = True
        print(f"\n移行済みと判断しました ({NEW_HEADERS[0]}={a1col(anchor+1)}列)。"
              f"数式と書式のみ再設定します")
    elif LEGACY_ALERT_HEADER in header:
        anchor = header.index(LEGACY_ALERT_HEADER)
        print(f"\n旧「{LEGACY_ALERT_HEADER}」列 = {a1col(anchor+1)}。"
              f"この列を「{NEW_HEADERS[0]}」に置き換え、直後に{N_INSERT}列挿入します")
    else:
        print(f"「{LEGACY_ALERT_HEADER}」も「{NEW_HEADERS[0]}」も見つかりません",
              file=sys.stderr)
        return 1

    if H_HINT not in header:
        print(f"ヘッダー {H_HINT!r} が見つかりません", file=sys.stderr)
        return 1

    # 2026-07-31 に削除した「梱包」「発注」列が残っていたら知らせる。
    # (数量が FBA梱包必要数 / 発注個数予測 と食い違って紛らわしいため廃止した)
    # 列の削除は取り消せないので、ここでは自動削除せず警告だけにとどめる。
    stale = [h for h in LEGACY_DROPPED_HEADERS if h in header]
    if stale:
        print(f"\n⚠ 廃止した列 {stale} が残っています。"
              f"FBA梱包必要数 / 発注個数予測 と数字が食い違うので、"
              f"手で削除してください", file=sys.stderr)

    # 挿入は「1列目 (=旧在庫警告 / 梱包)」の直後。1列目はその場で作り替える。
    insert_at = anchor + 1
    shift = 0 if args.formulas_only else N_INSERT

    def hindex(name: str) -> int:
        """ヘッダー文字列 → 列インデックス。

        '発注個数予測\\n(...)' のように改行で補足が付くヘッダーがあるので、
        完全一致 → 1行目の一致 → 前方一致 の順に探す。
        """
        if name in header:
            return header.index(name)
        for i, h in enumerate(header):
            if str(h).split("\n")[0].strip() == name:
                return i
        for i, h in enumerate(header):
            if h and str(h).startswith(name):
                return i
        raise ValueError(f"ヘッダー {name!r} が見つかりません")

    def newcol(name: str) -> str:
        """挿入後の列文字を返す (挿入位置以降は shift 個ずれる)。"""
        i0 = hindex(name)
        return a1col(i0 + 1 + (shift if i0 >= insert_at else 0))

    cols = {
        "days": newcol(H_DAYS),
        "total": newcol(H_TOTAL_STOCK),
        "fba": newcol(H_FBA),
        "rsl": newcol(H_RSL),
        "sc": newcol(H_SC),
        "need": newcol(H_PACK_NEED),
        "hint": newcol(H_HINT),
    }
    for i, key in enumerate(COLUMN_ORDER):
        cols[key] = a1col(anchor + 1 + i)
    cols["done"] = a1col(anchor + 1 + len(COLUMN_ORDER))

    print("\n=== 挿入後の参照列 ===")
    for k, v in cols.items():
        print(f"  {k:10s}: {v}")

    # --- 参照タブ / 平均行の解決 ---
    fba_date_idx0 = hindex(H_FBA_DATE)
    label_cache: dict[str, list[str]] = {}

    def labels(tab: str) -> list[str]:
        if tab not in label_cache:
            wst = sheets_retry(sp.worksheet, tab)
            label_cache[tab] = sheets_retry(wst.col_values, 1)
        return label_cache[tab]

    plans = []
    for r in range(2, len(forms) + 1):
        row = forms[r - 1]
        name = row[0] if row else ""
        if not name:
            continue
        f = row[fba_date_idx0] if len(row) > fba_date_idx0 else ""
        tab, base = first_data_ref(f)
        if not tab or not base:
            print(f"  !! R{r} {name}: 参照タブを特定できません", file=sys.stderr)
            return 1
        la = labels(tab)
        base_label = la[base - 1].strip() if 0 < base <= len(la) else ""
        if base_label != L_STOCK_FORECAST:
            print(f"  !! R{r} {name}: {tab} R{base} が {L_STOCK_FORECAST!r} ではなく "
                  f"{base_label!r} です", file=sys.stderr)
            return 1
        wk7, wavg = resolve_avg_rows(la, base)
        if not wk7 or not wavg:
            print(f"  !! R{r} {name}: {tab} で平均行を特定できません "
                  f"(wk7={wk7} wavg={wavg})", file=sys.stderr)
            return 1
        plans.append({"row": r, "name": name, "tab": tab,
                      "base": base, "wk7": wk7, "wavg": wavg})

    print(f"\n=== 対象 {len(plans)}行 ===")

    if args.dry_run:
        ex = plans[0]
        for key in COLUMN_ORDER:
            print(f"\n[dry-run] {COLUMN_SPECS[key][0]} ({ex['name']} R{ex['row']}):")
            print("  " + build_alert_formula(key, cols, ex["row"]))
        print(f"\n[dry-run] {H_HINT} ({ex['name']}):")
        print("  " + build_hint_formula(cols, ex["row"], ex["tab"],
                                        ex["wk7"], ex["wavg"]))
        return 0

    # --- 1) 列挿入 ---
    if not args.formulas_only:
        before = cf_ranges(sp, sheet_id)
        print(f"\n挿入前の条件付き書式: {len(before)}件")
        sheets_retry(sp.batch_update, {"requests": [{
            "insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": insert_at,
                          "endIndex": insert_at + N_INSERT},
                "inheritFromBefore": False}}]})
        print(f"{a1col(insert_at)}列の直後に{N_INSERT}列挿入しました")

        # 条件付き書式の範囲が挿入列へ波及していないか確認し、波及していたら戻す
        after = cf_ranges(sp, sheet_id)
        fix_reqs = []
        for idx, (b, a) in enumerate(zip(before, after)):
            want = [expected_after_insert(x, insert_at, N_INSERT)
                    for x in b["ranges"]]
            if want != a["ranges"]:
                print(f"  条件付き書式 #{idx} の範囲が波及 → 復元")
                print(f"    now : {a['ranges']}")
                print(f"    want: {want}")
                # 範囲の開始列(=CF数式の基準セル)が動く分だけ相対参照もずらす
                delta = (want[0].get("startColumnIndex", 0)
                         - a["ranges"][0].get("startColumnIndex", 0))
                fixed = retarget_rule(a, delta)
                rule = {"ranges": want}
                for k in ("booleanRule", "gradientRule"):
                    if k in fixed:
                        rule[k] = fixed[k]
                old_f = ((a.get("booleanRule", {}).get("condition", {})
                          .get("values") or [{}])[0].get("userEnteredValue"))
                new_f = ((rule.get("booleanRule", {}).get("condition", {})
                          .get("values") or [{}])[0].get("userEnteredValue"))
                if old_f != new_f:
                    print(f"    数式を {delta:+d}列ずらし: {old_f!r} → {new_f!r}")
                fix_reqs.append({"updateConditionalFormatRule": {
                    "sheetId": sheet_id, "index": idx, "rule": rule}})
        if fix_reqs:
            sheets_retry(sp.batch_update, {"requests": fix_reqs})
            print(f"  条件付き書式 {len(fix_reqs)}件を復元しました")
        else:
            print("  条件付き書式の波及なし")

    # --- 2) ヘッダーと数式 ---
    data = [{"range": f"{cols[key]}1", "values": [[COLUMN_SPECS[key][0]]]}
            for key in COLUMN_ORDER]
    data.append({"range": f"{cols['done']}1", "values": [[H_DONE]]})
    for p in plans:
        for key in COLUMN_ORDER:
            data.append({"range": f"{cols[key]}{p['row']}",
                         "values": [[build_alert_formula(key, cols, p["row"])]]})
        data.append({"range": f"{cols['hint']}{p['row']}",
                     "values": [[build_hint_formula(cols, p["row"], p["tab"],
                                                    p["wk7"], p["wavg"])]]})
    for i in range(0, len(data), 100):
        sheets_retry(ws.batch_update, [dict(u) for u in data[i:i + 100]],
                     value_input_option="USER_ENTERED")
    print(f"数式書き込み完了 ({len(data)}セル)")

    # --- 3) 書式 ---
    last_row = max(p["row"] for p in plans)

    def txt(color, bold):
        return {"foregroundColor": color,
                "foregroundColorStyle": {"rgbColor": color},
                "fontFamily": "Calibri", "bold": bold,
                "italic": False, "strikethrough": False, "underline": False}

    def cell_fmt(bg, align, color, bold):
        return {"userEnteredFormat": {
            "backgroundColor": bg,
            "backgroundColorStyle": {"rgbColor": bg},
            "horizontalAlignment": align,
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
            "textFormat": txt(color, bold)}}

    FIELDS = ("userEnteredFormat(backgroundColor,backgroundColorStyle,"
              "horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)")

    def col_range(c0: int, n: int = 1, r0: int = 1, r1: int | None = None):
        return {"sheetId": sheet_id, "startRowIndex": r0,
                "endRowIndex": r1 if r1 is not None else last_row,
                "startColumnIndex": c0, "endColumnIndex": c0 + n}

    done_idx = anchor + len(COLUMN_ORDER)
    hint_idx = anchor + N_NEW

    reqs = [
        # ヘッダー (販売調整/対応済み)
        {"repeatCell": {
            "range": col_range(anchor, N_NEW, 0, 1),
            "cell": cell_fmt(HEADER_BG, "CENTER", WHITE, True),
            "fields": FIELDS}},
        # アラート列のデータ行: 中央寄せ・黒字・白地 (隣接列からの引き継ぎ防止)。
        # 表示は必ず文字列なので、旧「在庫警告」列から引き継いだ日付書式を
        # TEXT に戻しておく (数値が紛れ込んだときに日付として化けるのを防ぐ)。
        {"repeatCell": {
            "range": col_range(anchor, len(COLUMN_ORDER)),
            "cell": {"userEnteredFormat": dict(
                cell_fmt(DATA_BG, "CENTER", BLACK, False)["userEnteredFormat"],
                numberFormat={"type": "TEXT", "pattern": "@"})},
            "fields": FIELDS + ",userEnteredFormat.numberFormat"}},
        # 対応済み: 手入力欄。薄いグレー地 + 日付書式 M/d
        {"repeatCell": {
            "range": col_range(done_idx),
            "cell": {"userEnteredFormat": {
                "backgroundColor": DONE_INPUT_BG,
                "backgroundColorStyle": {"rgbColor": DONE_INPUT_BG},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
                "numberFormat": {"type": "DATE", "pattern": DONE_DATE_FORMAT},
                "textFormat": txt(BLACK, False)}},
            "fields": FIELDS + ",userEnteredFormat.numberFormat"}},
        # 対応済みヘッダーの説明メモ
        {"repeatCell": {
            "range": col_range(done_idx, 1, 0, 1),
            "cell": {"note": DONE_HEADER_NOTE},
            "fields": "note"}},
        # 調整の目安のデータ行: 左寄せ・黒字・白地
        {"repeatCell": {
            "range": col_range(hint_idx),
            "cell": cell_fmt(DATA_BG, "LEFT", BLACK, False),
            "fields": FIELDS}},
        # 列幅
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": anchor,
                      "endIndex": anchor + len(COLUMN_ORDER)},
            "properties": {"pixelSize": COL_WIDTH_ACT}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": done_idx, "endIndex": done_idx + 1},
            "properties": {"pixelSize": COL_WIDTH_DONE}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": hint_idx, "endIndex": hint_idx + 1},
            "properties": {"pixelSize": COL_WIDTH_HINT}, "fields": "pixelSize"}},
    ]
    sheets_retry(sp.batch_update, {"requests": reqs})
    print("書式設定完了")

    # --- 4) 条件付き書式 ---
    # 既存のアラート用ルール (旧「在庫警告」1列分 / 旧3列分) を消して張り直す。
    existing = cf_ranges(sp, sheet_id)
    ours_texts = set(LEGACY_MATCH_TEXTS)
    for key in COLUMN_ORDER:
        ours_texts |= {s[1] for s in COLUMN_SPECS[key][1]}
    ours_texts |= {M_DONE, M_OVERDUE}
    our_cols = range(anchor, anchor + len(COLUMN_ORDER))

    def is_ours(rule: dict) -> bool:
        cond = rule.get("booleanRule", {}).get("condition", {})
        vals = [v.get("userEnteredValue") or "" for v in cond.get("values", [])]
        if cond.get("type") == "TEXT_CONTAINS" and any(v in ours_texts
                                                       for v in vals):
            return True
        # 前回この関数が張った CUSTOM_FORMULA (3列の範囲に閉じているもの)
        if cond.get("type") == "CUSTOM_FORMULA" and any(
                f'"{t}"' in v for v in vals for t in ours_texts):
            return all(rg.get("startColumnIndex") in our_cols
                       and rg.get("endColumnIndex", 0) - 1 in our_cols
                       for rg in rule.get("ranges", []))
        return False

    del_reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
                for i, r in reversed(list(enumerate(existing))) if is_ours(r)]
    if del_reqs:
        sheets_retry(sp.batch_update, {"requests": del_reqs})
        print(f"既存のアラート用ルール {len(del_reqs)}件を削除しました")

    # 判定文字列は絵文字を含めない (日本語部分で一致させる)。
    # 「✓ 済 8/1」のように日付が埋め込まれるため TEXT_CONTAINS では書きづらい。
    # 除外語も指定できる CUSTOM_FORMULA で統一する。
    def match_formula(col: str, match: str, exclude: tuple) -> str:
        cond = f'ISNUMBER(SEARCH("{match}",${col}2))'
        for x in exclude:
            cond += f',NOT(ISNUMBER(SEARCH("{x}",${col}2)))'
        return f"=AND({cond})"

    rules = []
    # 対応済み (✓済 / ⚠未着) を先に置く → 同じセルに複数一致したとき先勝ち
    for match, color in ((M_OVERDUE, "red"), (M_DONE, "grey")):
        ranges = [col_range(anchor + i) for i in range(len(COLUMN_ORDER))]
        formulas = [match_formula(cols[key], match, ()) for key in COLUMN_ORDER]
        # 範囲ごとに基準セルの列が違うため、列ごとに1ルールずつ張る
        for rg, f in zip(ranges, formulas):
            rules.append((f"共通 {match}", color, [rg], f))
    for key in COLUMN_ORDER:
        col = cols[key]
        c0 = anchor + COLUMN_ORDER.index(key)
        for _k, match, exclude, sample, color, _sev in COLUMN_SPECS[key][1]:
            rules.append((f"{COLUMN_SPECS[key][0]} {sample}", color,
                          [col_range(c0)], match_formula(col, match, exclude)))

    cf_reqs = []
    for i, (label, color, ranges, formula) in enumerate(rules):
        bg = CF_COLORS[color]
        cf_reqs.append({"addConditionalFormatRule": {
            "index": i,
            "rule": {
                "ranges": ranges,
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": bg,
                               "backgroundColorStyle": {"rgbColor": bg}}}}}})
        print(f"  #{i} {label} → {color}  {formula}")
    sheets_retry(sp.batch_update, {"requests": cf_reqs})
    print(f"条件付き書式 {len(cf_reqs)}件を設定しました")

    print("✅ 販売調整 / 対応済み / 調整の目安 の設定完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
