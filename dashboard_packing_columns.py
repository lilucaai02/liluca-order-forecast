#!/usr/bin/env python3
"""ダッシュボード２に「FBA/RSL/SC 梱包必要数」3列を追加する。

運用ルール:
  在庫が残り45日分になった時点で30日分の梱包準備を始めたい。
  梱包・発送に15日かかるため、到着するのは今日+15日。
  そこから60日分をカバーしたいので、必要なのは今日+75日までの在庫。
  ただし欠品期間の需要は売り逃しで回収できないため、
  在庫切れ商品は60日分で頭打ちにする。

計算式 (各倉庫の「在庫予想」行を2点参照):
  A = 在庫予想の 今日+15日 の値   (梱包・発送を経て到着する時点の在庫)
  B = 在庫予想の 今日+75日 の値
  梱包必要数 = ROUNDUP(MAX(0, MIN(A, 0) - B))

  - A >= 0 : MIN(A,0)=0 なので -B。75日後の不足額をそのまま梱包する
  - A <  0 : A - B。15日後〜75日後の「需要 - 入庫予定」= 実質60日分で頭打ち

  「在庫予想」行は 前日在庫 + 入庫予定 - 販売予測 - 出庫予定 のチェーンで、
  販売予測にはイベント(セール・タイムセール仮置き)が織り込まれている。
  60/75日以内の入庫予定も自動的に差し引かれるため二重梱包を防げる。

参照行 (商品タブA列ラベルで特定):
  FBA梱包必要数 → 「在庫予想」
  RSL梱包必要数 → 「RSL在庫予想」
  SC梱包必要数  → 「Stock Crew在庫予想」
  その倉庫の行が無い商品は「−」(全角ハイフン)を表示する。

使い方:
  python3 dashboard_packing_columns.py --dry-run
  python3 dashboard_packing_columns.py
  python3 dashboard_packing_columns.py --formulas-only   # 列挿入済みの再計算用
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

DEST_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
DASH = "ダッシュボード２"

# 挿入位置の基準となる既存ヘッダー (この列の直後に3列挿入)
ANCHOR_HEADER = "発注中個数"

# 参照先を特定するためのヘッダー (在庫切れ予測日は「在庫予想」系の行を参照している)
HEADER_FBA_DATE = "FBA在庫切れ予測日"

NEW_HEADERS = ["FBA梱包必要数", "RSL梱包必要数", "SC梱包必要数"]
LABELS = ["在庫予想", "RSL在庫予想", "Stock Crew在庫予想"]

DAYS_LEAD = 15    # 梱包・発送にかかる日数
DAYS_TOTAL = 75   # 15 + 60日分

DASH_MARK = "−"   # 全角ハイフン (他列の未使用倉庫表示と同じ)

REF_RE = re.compile(r"'([^']+)'!\$(\d+):\$(\d+)")

# --- 書式 (既存の「発注」グループ U/V 列に合わせる) ---
HEADER_BG = {"red": 0.21960784, "green": 0.4627451, "blue": 0.11372549}
DATA_BG = {"red": 0.8509804, "green": 0.91764706, "blue": 0.827451}
WHITE = {"red": 1, "green": 1, "blue": 1}
BLACK = {"red": 0, "green": 0, "blue": 0}
COL_WIDTH = 62


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
    """対象シートの条件付き書式ルール一覧 (index順の ranges) を返す。"""
    meta = sheets_retry(
        sp.fetch_sheet_metadata,
        params={"fields": "sheets(properties(sheetId,title),conditionalFormats)"})
    for x in meta["sheets"]:
        if x["properties"]["sheetId"] == sheet_id:
            return x.get("conditionalFormats", [])
    return []


def expected_after_insert(rng: dict, insert_at: int, count: int) -> dict:
    """列挿入後にあるべき範囲を計算する。

    Sheets は挿入位置がちょうど範囲の先頭にあるとき範囲を「広げて」しまう
    ことがある (過去にL列の赤字ルールが挿入列へ波及した事例あり)。
    本来は次の規則に従うべきなので、これを期待値として突き合わせる。
      s >= insert_at            → 右へずらす
      e <= insert_at            → そのまま
      s < insert_at < e         → 本当にまたいでいるので広げる
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


def build_formula(tab: str, row: int) -> str:
    t = f"'{tab}'"
    def at(offset: int) -> str:
        return (f"N(INDEX({t}!${row}:${row},1,"
                f"MATCH(TODAY()+{offset},{t}!$1:$1,0)))")
    return (f'=IFERROR(ROUNDUP(MAX(0,MIN({at(DAYS_LEAD)},0)-{at(DAYS_TOTAL)})),'
            f'"{DASH_MARK}")')


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
    anchor_idx0 = header.index(ANCHOR_HEADER)          # 0-based
    insert_at = anchor_idx0 + 1                        # 0-based 挿入位置

    already = [h for h in NEW_HEADERS if h in header]
    if already and not args.formulas_only:
        print(f"既に {already} が存在します。--formulas-only を使ってください",
              file=sys.stderr)
        return 1

    # --- 参照行の解決 ---
    fba_date_idx0 = header.index(HEADER_FBA_DATE)
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
        if base_label != LABELS[0]:
            print(f"  !! R{r} {name}: {tab} R{base} が {LABELS[0]!r} ではなく "
                  f"{base_label!r} です", file=sys.stderr)
            return 1
        # ブロック内(base から下方12行)でラベル検索。次の「在庫予想」で打ち切り
        found: dict[str, int] = {LABELS[0]: base}
        for rr in range(base + 1, min(base + 13, len(la) + 1)):
            lab = la[rr - 1].strip()
            if lab == LABELS[0]:
                break
            if lab in LABELS and lab not in found:
                found[lab] = rr
        plans.append({"row": r, "name": name, "tab": tab,
                      "rows": [found.get(x) for x in LABELS]})

    print(f"\n=== 対象 {len(plans)}行 ===")
    for p in plans:
        print(f"  R{p['row']:2d} {p['name']:20s} {p['tab']:14s} "
              f"在庫予想={p['rows'][0]} RSL={p['rows'][1]} SC={p['rows'][2]}")
    missing = {LABELS[i]: [p["name"] for p in plans if not p["rows"][i]]
               for i in range(3)}
    print("\n参照行が無い商品:")
    for k, v in missing.items():
        print(f"  {k}: {v}")

    if args.dry_run:
        cols = [a1col(insert_at + 1 + i) for i in range(3)]
        print(f"\n[dry-run] 挿入位置: {ANCHOR_HEADER}({a1col(anchor_idx0+1)})列の直後 "
              f"→ 新列 {cols}")
        ex = plans[0]
        print(f"[dry-run] 数式例 ({ex['name']} FBA):")
        print("  " + build_formula(ex["tab"], ex["rows"][0]))
        return 0

    # --- 1) 列挿入 ---
    if not args.formulas_only:
        before = cf_ranges(sp, sheet_id)
        sheets_retry(sp.batch_update, {"requests": [{
            "insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": insert_at, "endIndex": insert_at + 3},
                "inheritFromBefore": False}}]})
        print(f"\n{a1col(insert_at)}列の直後に3列挿入しました")

        # 条件付き書式の範囲が挿入列へ波及していないか確認し、波及していたら戻す
        after = cf_ranges(sp, sheet_id)
        fix_reqs = []
        for idx, (b, a) in enumerate(zip(before, after)):
            want = [expected_after_insert(r, insert_at, 3) for r in b["ranges"]]
            if want != a["ranges"]:
                print(f"  条件付き書式 #{idx} の範囲が波及 → 復元")
                print(f"    now : {a['ranges']}")
                print(f"    want: {want}")
                rule = {"ranges": want}
                for k in ("booleanRule", "gradientRule"):
                    if k in a:
                        rule[k] = a[k]
                fix_reqs.append({"updateConditionalFormatRule": {
                    "sheetId": sheet_id, "index": idx, "rule": rule}})
        if fix_reqs:
            sheets_retry(sp.batch_update, {"requests": fix_reqs})
            print(f"  条件付き書式 {len(fix_reqs)}件を復元しました")
        else:
            print("  条件付き書式の波及なし")

    new_cols = [a1col(insert_at + 1 + i) for i in range(3)]
    print(f"新列: {new_cols}")

    # --- 2) ヘッダーと数式 ---
    data = [{"range": f"{new_cols[i]}1", "values": [[NEW_HEADERS[i]]]}
            for i in range(3)]
    for p in plans:
        for i in range(3):
            row = p["rows"][i]
            v = build_formula(p["tab"], row) if row else DASH_MARK
            data.append({"range": f"{new_cols[i]}{p['row']}", "values": [[v]]})
    for i in range(0, len(data), 100):
        sheets_retry(ws.batch_update, data[i:i + 100],
                     value_input_option="USER_ENTERED")
    print(f"数式書き込み完了 ({len(data)}セル)")

    # --- 3) 書式 ---
    last_row = max(p["row"] for p in plans)
    reqs = [
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": insert_at,
                      "endColumnIndex": insert_at + 3},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_BG,
                "backgroundColorStyle": {"rgbColor": HEADER_BG},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
                "textFormat": {"foregroundColor": WHITE,
                               "foregroundColorStyle": {"rgbColor": WHITE},
                               "fontFamily": "Calibri", "bold": True}}},
            "fields": ("userEnteredFormat(backgroundColor,backgroundColorStyle,"
                       "horizontalAlignment,verticalAlignment,wrapStrategy,"
                       "textFormat)")}},
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "endRowIndex": last_row,
                      "startColumnIndex": insert_at,
                      "endColumnIndex": insert_at + 3},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                "backgroundColor": DATA_BG,
                "backgroundColorStyle": {"rgbColor": DATA_BG},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "textFormat": {"foregroundColor": BLACK,
                               "foregroundColorStyle": {"rgbColor": BLACK},
                               "fontFamily": "Calibri", "bold": False,
                               "italic": False, "strikethrough": False,
                               "underline": False}}},
            "fields": ("userEnteredFormat(numberFormat,backgroundColor,"
                       "backgroundColorStyle,horizontalAlignment,"
                       "verticalAlignment,textFormat)")}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": insert_at, "endIndex": insert_at + 3},
            "properties": {"pixelSize": COL_WIDTH},
            "fields": "pixelSize"}},
    ]
    sheets_retry(sp.batch_update, {"requests": reqs})
    print("書式設定完了")
    print("✅ 梱包必要数3列 追加完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
