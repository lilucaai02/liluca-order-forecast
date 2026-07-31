#!/usr/bin/env python3
"""ダッシュボード２に「在庫警告」「調整の目安」2列を追加する。

運用ルール (ユーザーの前提):
  在庫が30日分になった時点で補充が到着している想定で回している。
  つまり「FBA残り日数が30日を切っている」= 予定どおりなら入庫しているはずの
  時点で入庫していない = 供給が間に合っていない、というサイン。
  1ヶ月の猶予があればセールを調整して在庫を延ばせるので、
  30日を切った時点でアラートを出して判断材料にする。
  (入庫の見込みが立っていれば無視してよい。あくまで判断材料)

--- 在庫警告 (1列目) ---
使う値:
  在庫日数   = FBA残り日数            (C列)
  手元在庫   = 総在庫 - FBA - RSL - SC (= 荒瀬 + 事務所 + 中国 + 移動中)
  発注中     = 発注中個数
  梱包必要数 = FBA梱包必要数

判定 (上から順に評価。2026-07-31 の変更では条件は一切いじらず表示文言だけ差し替えた):
  在庫日数が数値でない or 30より大きい       → ""            (アラートなし)
  在庫日数 <= 0                             → ❌ 欠品中       (赤)
  手元在庫 >= 梱包必要数 かつ 在庫日数 <= 15 → 🔥 急いで梱包   (橙 / 自力で解決できる)
  手元在庫 >= 梱包必要数                     → 🚚 梱包する     (黄)
  発注中 > 0 かつ 在庫日数 <= 15            → 🛑 セール全停止 (赤)
  発注中 > 0                                → 🔽 セール減らす (橙)
  上記以外 (手元も発注中もなし)              → 🏭 すぐ発注     (赤)

  アイコンで「何をするか」、背景色で「いつまでに」を表す。
  🏭 = 工場に頼む / 🚚 🔥 = 自分で動かす / 🛑 🔽 = 売る量を絞る。
  文言と色の対応は stock_alert_labels.py に集約している。

  C列は "✖️" や "−" の文字列になることがあるため ISNUMBER で弾く。
  全体を IFERROR でラップし、エラー時は空欄。

--- 調整の目安 (2列目) ---
在庫警告が空欄でない商品にのみ表示:
  「このまま◯日 ／ 停止で◯日 ／ 14日持たせるには◯個/日」

  このまま◯日 = FBA残り日数 (C列) …… 何もしなければ在庫が尽きるまでの日数
  停止で◯日   = FBA在庫数 ÷ 抑制ペース (切り捨て)
    抑制ペース = 該当商品タブの
                 「直近7平日セール以外平均」と「直近セール以外加重平均」の
                 大きい方 (安全側 = 消費が多い前提で見積もる)
    抑制ペースが0以下なら「停止で在庫維持可」
  14日持たせるには◯個/日 = FBA在庫数 ÷ 14 (小数第1位)
    目標日数は stock_alert_labels.HINT_TARGET_DAYS の1箇所で定義。
    以前は60日だったが、30日で警告が出る運用では警告時点の在庫が
    30日分しかなく60日は物理的に持たないため、意味のない数字だった。

参照行は商品タブのA列ラベルで特定する (config の行番号は当てにしない)。

使い方:
  python3 dashboard_stock_alert_columns.py --dry-run
  python3 dashboard_stock_alert_columns.py
  python3 dashboard_stock_alert_columns.py --formulas-only  # 列挿入済みの再設定用
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
    ALERT_SPECS,
    A_OUT_OF_STOCK,
    A_ORDER_NOW,
    A_PACK,
    A_PACK_URGENT,
    A_SALE_REDUCE,
    A_SALE_STOP,
    HINT_TARGET_DAYS,
    LEGACY_MATCH_TEXTS,
)

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
DASH = "ダッシュボード２"

# 挿入位置の基準となる既存ヘッダー (この列の直後に2列挿入)
ANCHOR_HEADER = "総数残り日数"

NEW_HEADERS = ["在庫警告", "調整の目安"]
N_NEW = len(NEW_HEADERS)

# 判定・表示に使う既存ヘッダー (実際の列位置はヘッダー文字列から解決する)
H_DAYS = "FBA残り日数"
H_FBA_DATE = "FBA在庫切れ予測日"      # 参照タブ/行の解決に使う
H_TOTAL_STOCK = ""                    # 総在庫 (ヘッダーが空欄の列)
H_FBA = "FBA在庫数"
H_RSL = "RSL在庫数"
H_SC = "ストッククルー在庫数"
H_ORDERING = "発注中個数"
H_PACK = "FBA梱包必要数"

# 商品タブ側のA列ラベル
L_STOCK_FORECAST = "在庫予想"          # FBA在庫切れ予測日が参照している行
L_AMAZON_SALES = "amazonFBA販売実績"
L_WEEKDAY_AVG = "直近7平日セール以外平均"
L_WEIGHTED_AVG = "直近セール以外加重平均"

DAYS_ALERT = 30    # これ以下でアラート開始 (補充が届いているはずの水準)
DAYS_URGENT = 15   # これ以下は至急
# 「◯日持たせるには」の目標日数は stock_alert_labels.HINT_TARGET_DAYS (=14)

REF_RE = re.compile(r"'([^']+)'!\$(\d+):\$(\d+)")

# --- 書式 ------------------------------------------------------------------
# ヘッダー: 直前の「残り日数」グループ (C〜F) と同じ濃紺 + 白太字
HEADER_BG = {"red": 0.043137256, "green": 0.3254902, "blue": 0.5803922}
WHITE = {"red": 1, "green": 1, "blue": 1}
BLACK = {"red": 0, "green": 0, "blue": 0}
# データ行は白地。隣接列の背景を引き継がせず、条件付き書式の色を素直に見せる
DATA_BG = WHITE

# 条件付き書式の背景色 (Google 標準の淡色)
CF_RED = {"red": 0.95686275, "green": 0.8, "blue": 0.8}          # #f4cccc
CF_ORANGE = {"red": 0.9882353, "green": 0.8980392, "blue": 0.8039216}  # #fce5cd
CF_YELLOW = {"red": 1, "green": 0.9490196, "blue": 0.8}          # #fff2cc
CF_COLORS = {"red": CF_RED, "orange": CF_ORANGE, "yellow": CF_YELLOW}

COL_WIDTH_ALERT = 130
COL_WIDTH_HINT = 250


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
    別の列を指してしまう (今回 G2:G62 → I2:I62 に戻したとき、自分自身を指す
    はずの G2 が2列左 = 新設した在庫警告列を指してしまった)。
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


def build_alert_formula(cols: dict, r: int) -> str:
    """在庫警告の数式。cols は挿入後の列文字 (例 {"days": "C", ...})。"""
    days = f"${cols['days']}{r}"
    hand = (f"N(${cols['total']}{r})-N(${cols['fba']}{r})"
            f"-N(${cols['rsl']}{r})-N(${cols['sc']}{r})")
    pack = f"N(${cols['pack']}{r})"
    order = f"N(${cols['ordering']}{r})"
    # 条件式は 2026-07-31 の変更でも一切いじっていない (表示文言のみ差し替え)
    return (
        f'=IFERROR('
        f'IF(NOT(ISNUMBER({days})),"",'
        f'IF({days}>{DAYS_ALERT},"",'
        f'IF({days}<=0,"{A_OUT_OF_STOCK}",'
        f'IF(AND({hand}>={pack},{days}<={DAYS_URGENT}),"{A_PACK_URGENT}",'
        f'IF({hand}>={pack},"{A_PACK}",'
        f'IF(AND({order}>0,{days}<={DAYS_URGENT}),"{A_SALE_STOP}",'
        f'IF({order}>0,"{A_SALE_REDUCE}",'
        f'"{A_ORDER_NOW}")))))))'
        f',"")')


def build_hint_formula(cols: dict, r: int, tab: str, wk7: int, wavg: int) -> str:
    """調整の目安の数式。

    「このまま◯日 ／ 停止で◯日 ／ 14日持たせるには◯個/日」
      このまま = FBA残り日数 (C列) をそのまま
      停止で   = FBA在庫数 ÷ 抑制ペース (切り捨て)
      14日     = HINT_TARGET_DAYS
    """
    t = f"'{tab}'"

    def at(row: int) -> str:
        return f"N(INDEX({t}!${row}:${row},1,MATCH(TODAY(),{t}!$1:$1,0)))"

    pace = f"MAX({at(wk7)},{at(wavg)})"
    fba = f"N(${cols['fba']}{r})"
    days = f"${cols['days']}{r}"
    alert = f"${cols['alert']}{r}"
    return (
        f'=IFERROR(IF({alert}="","",'
        f'"このまま"&TEXT(ROUND({days},0),"0")&"日 ／ "&'
        f'IF({pace}<=0,"停止で在庫維持可",'
        f'"停止で"&ROUNDDOWN({fba}/{pace},0)&"日")'
        f'&" ／ {HINT_TARGET_DAYS}日持たせるには"'
        f'&TEXT(ROUND({fba}/{HINT_TARGET_DAYS},1),"0.0")&"個/日"),"")')


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

    if ANCHOR_HEADER not in header:
        print(f"ヘッダー {ANCHOR_HEADER!r} が見つかりません", file=sys.stderr)
        return 1
    already = [h for h in NEW_HEADERS if h in header]
    if already and not args.formulas_only:
        print(f"既に {already} が存在します。--formulas-only を使ってください",
              file=sys.stderr)
        return 1

    if args.formulas_only:
        # 既に挿入済み: 新列の実位置をヘッダーから引く
        insert_at = header.index(NEW_HEADERS[0])
    else:
        insert_at = header.index(ANCHOR_HEADER) + 1   # 0-based 挿入位置

    def newcol(name: str) -> str:
        """挿入後の列文字を返す (挿入位置以降は N_NEW 個ずれる)。"""
        i0 = header.index(name)
        if args.formulas_only:
            return a1col(i0 + 1)
        return a1col(i0 + 1 + (N_NEW if i0 >= insert_at else 0))

    cols = {
        "days": newcol(H_DAYS),
        "total": newcol(H_TOTAL_STOCK),
        "fba": newcol(H_FBA),
        "rsl": newcol(H_RSL),
        "sc": newcol(H_SC),
        "ordering": newcol(H_ORDERING),
        "pack": newcol(H_PACK),
        "alert": a1col(insert_at + 1),
        "hint": a1col(insert_at + 2),
    }
    print("\n=== 挿入後の参照列 ===")
    for k, v in cols.items():
        print(f"  {k:9s}: {v}")

    # --- 参照タブ / 平均行の解決 ---
    fba_date_idx0 = header.index(H_FBA_DATE)
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
    for p in plans:
        print(f"  R{p['row']:2d} {p['name']:20s} {p['tab']:16s} "
              f"在庫予想={p['base']:4d} 7平日平均={p['wk7']:4d} 加重平均={p['wavg']:4d}")

    if args.dry_run:
        ex = plans[0]
        print(f"\n[dry-run] 挿入位置: {ANCHOR_HEADER}"
              f"({a1col(header.index(ANCHOR_HEADER)+1)})列の直後 → "
              f"新列 {cols['alert']}, {cols['hint']}")
        print(f"\n[dry-run] 在庫警告 ({ex['name']}):")
        print("  " + build_alert_formula(cols, ex["row"]))
        print(f"\n[dry-run] 調整の目安 ({ex['name']}):")
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
                          "endIndex": insert_at + N_NEW},
                "inheritFromBefore": False}}]})
        print(f"{a1col(insert_at)}列の直後に{N_NEW}列挿入しました")

        # 条件付き書式の範囲が挿入列へ波及していないか確認し、波及していたら戻す
        after = cf_ranges(sp, sheet_id)
        fix_reqs = []
        for idx, (b, a) in enumerate(zip(before, after)):
            want = [expected_after_insert(x, insert_at, N_NEW) for x in b["ranges"]]
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
    data = [{"range": f"{cols['alert']}1", "values": [[NEW_HEADERS[0]]]},
            {"range": f"{cols['hint']}1", "values": [[NEW_HEADERS[1]]]}]
    for p in plans:
        data.append({"range": f"{cols['alert']}{p['row']}",
                     "values": [[build_alert_formula(cols, p["row"])]]})
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

    reqs = [
        # ヘッダー (2列)
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": insert_at,
                      "endColumnIndex": insert_at + N_NEW},
            "cell": cell_fmt(HEADER_BG, "CENTER", WHITE, True),
            "fields": FIELDS}},
        # 在庫警告のデータ行: 中央寄せ・黒字・白地
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "endRowIndex": last_row,
                      "startColumnIndex": insert_at,
                      "endColumnIndex": insert_at + 1},
            "cell": cell_fmt(DATA_BG, "CENTER", BLACK, False),
            "fields": FIELDS}},
        # 調整の目安のデータ行: 左寄せ・黒字・白地
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "endRowIndex": last_row,
                      "startColumnIndex": insert_at + 1,
                      "endColumnIndex": insert_at + N_NEW},
            "cell": cell_fmt(DATA_BG, "LEFT", BLACK, False),
            "fields": FIELDS}},
        # 列幅
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": insert_at, "endIndex": insert_at + 1},
            "properties": {"pixelSize": COL_WIDTH_ALERT}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": insert_at + 1,
                      "endIndex": insert_at + N_NEW},
            "properties": {"pixelSize": COL_WIDTH_HINT}, "fields": "pixelSize"}},
    ]
    sheets_retry(sp.batch_update, {"requests": reqs})
    print("書式設定完了")

    # --- 4) 条件付き書式 (在庫警告列のデータ行のみ) ---
    alert_range = {"sheetId": sheet_id, "startRowIndex": 1,
                   "endRowIndex": last_row,
                   "startColumnIndex": insert_at,
                   "endColumnIndex": insert_at + 1}

    # 既存の在庫警告ルール (旧: 🔴🟠🟡 / 新: 文言) を消してから張り直す。
    # 旧ルールは絵文字を判定していたため、新文言には一致せず色が付かなくなる。
    existing = cf_ranges(sp, sheet_id)
    ours_values = set(LEGACY_MATCH_TEXTS) | {s[1] for s in ALERT_SPECS}

    def is_ours(rule: dict) -> bool:
        br = rule.get("booleanRule", {})
        cond = br.get("condition", {})
        if cond.get("type") != "TEXT_CONTAINS":
            return False
        vals = [v.get("userEnteredValue") for v in cond.get("values", [])]
        return any(v in ours_values for v in vals)

    del_reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
                for i, r in reversed(list(enumerate(existing))) if is_ours(r)]
    if del_reqs:
        sheets_retry(sp.batch_update, {"requests": del_reqs})
        print(f"既存の在庫警告ルール {len(del_reqs)}件を削除しました")

    # 判定文字列は絵文字を含めない (日本語部分で一致させる)。
    # どれも他の文言の部分文字列ではないので、評価順に依存しない。
    cf_reqs = []
    for disp, match, color, _sev in ALERT_SPECS:
        bg = CF_COLORS[color]
        cf_reqs.append({"addConditionalFormatRule": {
            "index": 0,
            "rule": {
                "ranges": [alert_range],
                "booleanRule": {
                    "condition": {"type": "TEXT_CONTAINS",
                                  "values": [{"userEnteredValue": match}]},
                    "format": {"backgroundColor": bg,
                               "backgroundColorStyle": {"rgbColor": bg}}}}}})
        print(f"  {disp} → {color} (TEXT_CONTAINS {match!r})")
    sheets_retry(sp.batch_update, {"requests": cf_reqs})
    print(f"条件付き書式 {len(cf_reqs)}件を設定しました "
          f"(範囲: {cols['alert']}2:{cols['alert']}{last_row})")

    print("✅ 在庫警告2列 追加完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
