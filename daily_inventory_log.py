#!/usr/bin/env python3
"""
毎朝5時に実行: 前日の在庫をGoogleスプレッドシートに追記する
シート構成:
  - 「日次在庫ログ」: 日付・MP・アカウント・SKU・ASIN・商品名・出荷可能数・合計数・状態
"""

import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from src.inventory import fetch_inventory, fetch_rakuten_inventory
from src.sp_client import SPClient
from src.rakuten_client import RakutenClient
from src.monitor import load_thresholds


SHEET_NAME = "日次在庫ログ"
HEADERS = ["日付", "MP", "アカウント", "SKU", "ASIN", "商品名", "出荷可能数", "合計数", "発注点", "状態"]


def get_all_inventory(settings: Settings) -> list[dict]:
    defaults, sku_configs = load_thresholds(settings.thresholds_file)
    # 前日の日付
    target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = []

    # Amazon
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = fetch_inventory(client)
            for item in items:
                t = sku_configs.get(item.seller_sku, defaults)
                q = item.fulfillable_quantity
                if q == 0:
                    status = "在庫切れ"
                elif q <= t.critical_level:
                    status = "危険"
                elif q <= t.reorder_point:
                    status = "要発注"
                else:
                    status = "正常"
                rows.append({
                    "日付": target_date,
                    "MP": "Amazon",
                    "アカウント": acc.name,
                    "SKU": item.seller_sku,
                    "ASIN": item.asin,
                    "商品名": item.product_name[:50],
                    "出荷可能数": q,
                    "合計数": item.total_quantity,
                    "発注点": t.reorder_point,
                    "状態": status,
                })
            print(f"[Amazon:{acc.name}] {len(items)}件取得")
        except Exception as e:
            print(f"[Amazon:{acc.name}] エラー: {e}", file=sys.stderr)

    # 楽天
    for acc in settings.get_rakuten_accounts():
        try:
            client = RakutenClient(acc)
            items = fetch_rakuten_inventory(client)
            for item in items:
                t = sku_configs.get(item.seller_sku, defaults)
                q = item.fulfillable_quantity
                status = "在庫切れ" if q == 0 else ("危険" if q <= t.critical_level else ("要発注" if q <= t.reorder_point else "正常"))
                rows.append({
                    "日付": target_date,
                    "MP": "楽天",
                    "アカウント": acc.name,
                    "SKU": item.seller_sku,
                    "ASIN": item.asin,
                    "商品名": item.product_name[:50],
                    "出荷可能数": q,
                    "合計数": item.total_quantity,
                    "発注点": t.reorder_point,
                    "状態": status,
                })
            print(f"[楽天:{acc.name}] {len(items)}件取得")
        except Exception as e:
            print(f"[楽天:{acc.name}] エラー: {e}", file=sys.stderr)

    return rows


def append_to_gsheet(rows: list[dict], spreadsheet_id: str, credentials_file: str) -> str:
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(credentials_file, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_id)

    # シートを取得 or 作成
    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=10000, cols=len(HEADERS))
        # ヘッダー行を作成
        ws.update(range_name="A1", values=[HEADERS])
        ws.format("A1:Z1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.17, "green": 0.34, "blue": 0.59},
            "horizontalAlignment": "CENTER",
        })
        ws.freeze(rows=1)
        print(f"シート「{SHEET_NAME}」を新規作成しました")

    # 既存データの最終行を確認
    existing = ws.col_values(1)  # A列(日付)
    next_row = len(existing) + 1

    # 追記
    data = [[row[h] for h in HEADERS] for row in rows]
    ws.update(range_name=f"A{next_row}", values=data)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    print(f"→ {len(rows)}行を追記しました（{next_row}行目〜）")
    print(f"→ {url}")
    return url


def main():
    settings = Settings()

    if not settings.google_credentials_file or not settings.google_spreadsheet_id:
        print("エラー: .envに以下を設定してください:")
        print("  GOOGLE_CREDENTIALS_FILE=path/to/service-account.json")
        print("  GOOGLE_SPREADSHEET_ID=your-spreadsheet-id")
        sys.exit(1)

    print(f"=== 日次在庫ログ [{datetime.now().strftime('%Y-%m-%d %H:%M')}] ===")
    rows = get_all_inventory(settings)

    if not rows:
        print("取得データなし")
        sys.exit(0)

    print(f"\n合計 {len(rows)} 件をGoogleスプレッドシートに追記中...")
    append_to_gsheet(rows, settings.google_spreadsheet_id, settings.google_credentials_file)
    print("完了")


if __name__ == "__main__":
    main()
