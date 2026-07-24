#!/usr/bin/env python3
"""
発注予測スプレッドシートの商品別タブの構造を自動検出する。

各タブの A列を走査し、ASIN・商品コード・各種行（amazon販売予想、amazonFBA販売実績、
FBA在庫予想/実績、在庫変更（数量）、FROM、TO）の行番号を見つけ出し、
tab_blocks_config.py 用の Python 辞書形式で出力する。

正規ブロックの判定: ブロック内に
  - amazon販売予想（R+8 付近）
  - amazonFBA販売実績（R+9 付近）
  - FBA在庫予想・FBA在庫実績
  - 在庫変更（数量）・FROM・TO
が揃っているブロックのみ抽出。MP-01-MHP のような簡略レイアウトは除外。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings

DEFAULT_DEST_SPREADSHEET_ID = "1mbZlalllDfJDbUmxUx-DNe3Q9uv1cEIhqE4PqFN6C7U"

# 対象タブ名（全11タブ。楽天行追加のため全タブを再スキャン）
TARGET_TABS = [
    "マウスピース(在庫)",
    "DS-01 (在庫) ",
    "GC-01(在庫)",
    "GC-02(在庫)",
    "TG-01(在庫)",
    "TG-02(在庫)",
    "PCI-01",
    "WB-01(在庫)",
    "WB-02",
    "TS-01",
    "PG-01",
]


ASIN_RE = re.compile(r'^B0[A-Z0-9]{8}$')


def detect_blocks(col_a: list[str], tab_name: str) -> list[dict]:
    """A列から商品ブロックを検出。"""
    # ASIN 行を抽出
    asin_rows: list[tuple[int, str]] = []
    for i, v in enumerate(col_a, start=1):
        if ASIN_RE.match(v.strip()):
            asin_rows.append((i, v.strip()))

    blocks = []
    for idx, (asin_row, asin) in enumerate(asin_rows):
        # 次のASINまで（最後ならファイル末尾まで）
        end_row = asin_rows[idx + 1][0] - 1 if idx + 1 < len(asin_rows) else len(col_a)

        # ブロック内の各ラベル行を検索
        rows_in_block = {}
        required_labels = {
            "amazon販売予想": "sales_forecast_row",
            "amazonFBA販売実績": "sales_row",
            "FBA在庫予想": "forecast_row",
            "FBA在庫実績": "stock_row",
            "在庫変更（数量）": "change_qty_row",
            "FROM": "from_row",
            "TO": "to_row",
        }
        optional_labels = {
            "楽天販売予想": "rakuten_sales_forecast_row",
            "楽天販売実績": "rakuten_sales_row",
            "RSL在庫予想": "rsl_forecast_row",
            "RSL在庫実績": "rsl_stock_row",
            "Yahoo販売予想": "yahoo_sales_forecast_row",
            "Yahoo販売実績": "yahoo_sales_row",
            "Stock Crew在庫予想": "stock_crew_forecast_row",
            "Stock Crew在庫実績": "stock_crew_stock_row",
        }
        labels_to_find = {**required_labels, **optional_labels}
        for r in range(asin_row, end_row + 1):
            label = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
            for label_target, key in labels_to_find.items():
                if label == label_target and key not in rows_in_block:
                    rows_in_block[key] = r

        # 必須行（Amazon系）が全て揃っているか判定（楽天は任意）
        if not all(k in rows_in_block for k in required_labels.values()):
            missing = [k for k in required_labels.values() if k not in rows_in_block]
            print(f"  [スキップ] {tab_name} ブロック ASIN={asin} (R{asin_row}): "
                  f"必須行不足 {missing}", file=sys.stderr)
            continue

        # 商品コードを探す: ASIN の直後 1〜3行以内で、空でなく既知ラベルでない値
        code = ""
        code_row = 0
        for r in range(asin_row + 1, min(asin_row + 5, end_row + 1)):
            v = col_a[r - 1].strip() if r - 1 < len(col_a) else ""
            if not v:
                continue
            # 既知ラベルや「週」「曜日」等は除外
            if v in ("週", "セール価格", "通常価格販売", "amazonイベント"):
                continue
            code = v
            code_row = r
            break

        if not code:
            print(f"  [スキップ] {tab_name} ブロック ASIN={asin} (R{asin_row}): 商品コードが取れない",
                  file=sys.stderr)
            continue

        blk = {
            "code": code, "asin": asin,
            "asin_row": asin_row, "code_row": code_row,
            **rows_in_block,
        }
        blocks.append(blk)

    return blocks


def format_block_py(blk: dict) -> str:
    """Python リテラル形式で1ブロック出力。"""
    base = (
        f'        {{"code": "{blk["code"]}", "asin": "{blk["asin"]}",\n'
        f'         "asin_row": {blk["asin_row"]}, "code_row": {blk["code_row"]},\n'
        f'         "sales_forecast_row": {blk["sales_forecast_row"]}, '
        f'"sales_row": {blk["sales_row"]},\n'
        f'         "forecast_row": {blk["forecast_row"]}, '
        f'"stock_row": {blk["stock_row"]},\n'
        f'         "change_qty_row": {blk["change_qty_row"]}, '
        f'"from_row": {blk["from_row"]}, "to_row": {blk["to_row"]}'
    )
    # 楽天系
    rk = []
    for k in ("rakuten_sales_forecast_row", "rakuten_sales_row",
              "rsl_forecast_row", "rsl_stock_row"):
        if k in blk:
            rk.append(f'"{k}": {blk[k]}')
    # Yahoo系
    yh = []
    for k in ("yahoo_sales_forecast_row", "yahoo_sales_row",
              "stock_crew_forecast_row", "stock_crew_stock_row"):
        if k in blk:
            yh.append(f'"{k}": {blk[k]}')
    extras = []
    if rk:
        extras.append(',\n         ' + ', '.join(rk))
    if yh:
        extras.append(',\n         ' + ', '.join(yh))
    return base + ''.join(extras) + '},'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest-id", default=DEFAULT_DEST_SPREADSHEET_ID,
                        help="対象スプレッドシートID")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_credentials_file:
        print("エラー: .env を確認", file=sys.stderr)
        sys.exit(1)

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
    sp = gc.open_by_key(args.dest_id)
    print(f"# 対象: {sp.title} ({args.dest_id})", file=sys.stderr)

    summary = []  # [(tab, [block dicts])]
    print("# === scan_tab_structure.py 出力 ===")
    print("# tab_blocks_config.py の TAB_BLOCKS にコピーしてください\n")

    for tab_name in TARGET_TABS:
        print(f"\n# ----- {tab_name} -----", file=sys.stderr)
        try:
            ws = sp.worksheet(tab_name)
        except Exception as e:
            print(f"  [エラー] タブが見つかりません: {e}", file=sys.stderr)
            continue

        col_a = ws.col_values(1)
        blocks = detect_blocks(col_a, tab_name)
        summary.append((tab_name, blocks))

        if not blocks:
            print(f"  → ブロック0件", file=sys.stderr)
            continue
        print(f"  → {len(blocks)} ブロック検出", file=sys.stderr)
        for b in blocks:
            print(f"    - {b['code']} (R{b['asin_row']}): "
                  f"stock=R{b['stock_row']}, sales=R{b['sales_row']}", file=sys.stderr)

        # Python 形式で出力
        print(f'    # ============================================================')
        print(f'    # {tab_name}')
        print(f'    # ============================================================')
        print(f'    "{tab_name}": [')
        for b in blocks:
            print(format_block_py(b))
        print(f'    ],')

    # まとめ
    print("\n\n=== サマリー ===", file=sys.stderr)
    for tab, blocks in summary:
        print(f"  {tab}: {len(blocks)} ブロック", file=sys.stderr)
        for b in blocks:
            print(f"    - {b['code']} (ASIN: {b['asin']})", file=sys.stderr)


if __name__ == "__main__":
    main()
