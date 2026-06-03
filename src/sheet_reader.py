#!/usr/bin/env python3
"""
既存スプレッドシートからの読み取り専用モジュール
- 発注予測スプレッドシート（ダッシュボードタブ）からLT・発注中個数を取得
- 荒瀬倉庫入出庫履歴から現在庫を計算
- 在庫発注タイミング（中国在庫）シートから仕入れ先在庫を取得

※ 楽天倉庫在庫は楽天RMS APIから直接取得するため、ここでは扱わない
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Optional

# スプレッドシートID（読み取り専用・変更しない）
FORECAST_SHEET_ID      = "1rt5m4SUSiYA5i7Loj5uxd4YqZuQWxQ2AnyFGKU3rs6E"   # 発注予測 のコピー
ARASE_SHEET_ID         = "1UijOeOrVBCLFHqVXq5ULSiaURdDxu6R40dhFeOSv00k"   # 荒瀬倉庫 のコピー
CHINA_STOCK_SHEET_ID   = "1Te_nYvPehY3ik2lyHWB8JqGhOJh5SI7OVrN6WFCX9dA"   # 在庫発注タイミング のコピー
OFFICE_STOCK_SHEET_ID  = "1Sz2CFCyEfg4_2FClYd8dzTj81DW7SuSvtYrNKiQQAvA"   # LILUCA事務所在庫表

# ダッシュボードタブの列マッピング（0-indexed）
# A=0, B=1, ..., G=6, H=7, I=8, J=9, K=10, L=11, M=12, N=13, O=14, P=15, Q=16, R=17
# ※ 新「発注予測のコピー」での実列構成に基づく
COL_SKU        = 1   # B列: 商品SKU
COL_ORDER_DATE = 6   # G列: 総数発注予測日（在庫切れ30日前-リードタイム）
COL_FBA_STOCK  = 7   # H列: FBA在庫数
COL_RSL_STOCK  = 9   # J列: RSL在庫数
COL_ORDER_QTY  = 11  # L列: 発注個数予測
COL_IN_TRANSIT = 12  # M列: 発注中個数
COL_LT         = 13  # N列: リードタイム（日数）
COL_FBA_DAYS   = 14  # O列: FBA残り日数
COL_RSL_DAYS   = 15  # P列: RSL残り日数
COL_SC_DAYS    = 16  # Q列: Stock Crew残り日数
COL_TOTAL_DAYS = 17  # R列: 総数残り日数


@dataclass
class SKUMasterData:
    """発注予測シートから取得したSKU別マスターデータ"""
    sku: str
    lt_days: int = 0            # リードタイム（日）
    in_transit: int = 0         # 発注中（移動中）個数
    order_qty: int = 0          # 発注推奨個数
    order_date: str = ""        # 発注基準日
    fba_days_left: Optional[int] = None   # FBA残り日数
    rsl_days_left: Optional[int] = None   # RSL残り日数
    total_days_left: Optional[int] = None # 総数残り日数


@dataclass
class AraseSKUStock:
    """荒瀬倉庫の現在庫"""
    sku: str
    qty: int = 0


def _get_gc(credentials_file: str):
    """gspread クライアントを取得"""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        credentials_file,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    return gspread.authorize(creds)


def _safe_int(val, default=0) -> int:
    """文字列や空値を安全にintに変換"""
    if val is None or val == "" or val == "-":
        return default
    try:
        # 「予測時間過ぎました」などの文字列は0扱い
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def _safe_days(val) -> Optional[int]:
    """残り日数を安全に変換（文字列エラーはNone）"""
    if val is None or val == "" or val == "-":
        return None
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None  # 「予測時間過ぎました」など


def load_sku_master(credentials_file: str) -> dict[str, SKUMasterData]:
    """
    発注予測スプレッドシートのダッシュボードタブから
    SKU別マスターデータを読み込む（読み取り専用）

    Returns:
        {sku_name: SKUMasterData}
    """
    result: dict[str, SKUMasterData] = {}
    try:
        gc = _get_gc(credentials_file)
        ss = gc.open_by_key(FORECAST_SHEET_ID)
        ws = ss.worksheet("ダッシュボード")

        # 全データ取得（値のみ）
        all_values = ws.get_all_values()

        # ヘッダー行を探す（B列が日付でない最初のデータ行 = row index 3以降）
        # Row 0: 空/カウント行, Row 1: ヘッダー行, Row 2: 空, Row 3以降: データ
        data_start = 3

        for row_idx, row in enumerate(all_values):
            if row_idx < data_start:
                continue

            # SKU列が空か空白ならスキップ
            if len(row) <= COL_SKU:
                continue
            sku = str(row[COL_SKU]).strip()
            if not sku or sku.startswith("2026") or sku.startswith("20"):
                continue  # 日付や空行をスキップ

            def get(col):
                return row[col] if len(row) > col else ""

            lt = _safe_int(get(COL_LT), default=0)
            in_transit = _safe_int(get(COL_IN_TRANSIT), default=0)
            order_qty = _safe_int(get(COL_ORDER_QTY), default=0)
            order_date = str(get(COL_ORDER_DATE)).strip()
            fba_days = _safe_days(get(COL_FBA_DAYS))
            rsl_days = _safe_days(get(COL_RSL_DAYS))
            total_days = _safe_days(get(COL_TOTAL_DAYS))

            if lt == 0 and in_transit == 0 and not sku:
                continue

            result[sku] = SKUMasterData(
                sku=sku,
                lt_days=lt,
                in_transit=in_transit,
                order_qty=order_qty,
                order_date=order_date,
                fba_days_left=fba_days,
                rsl_days_left=rsl_days,
                total_days_left=total_days,
            )

        print(f"[sheet_reader] 発注予測ダッシュボード: {len(result)}件 読み込み完了", file=sys.stderr)

    except Exception as e:
        import traceback
        print(f"[sheet_reader] 発注予測読み込みエラー: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return result


def load_arase_stock(credentials_file: str) -> dict[str, int]:
    """
    荒瀬倉庫入出庫履歴から現在庫を計算（読み取り専用）
    増減を累計して在庫数を算出

    Returns:
        {sku_name: current_qty}
    """
    stock: dict[str, int] = {}
    try:
        gc = _get_gc(credentials_file)
        ss = gc.open_by_key(ARASE_SHEET_ID)
        ws = ss.worksheet("入出庫履歴")

        all_values = ws.get_all_values()

        # ヘッダー行スキップ（row 0）
        # 列: A=番号, B=記入日付, C=発送日, D=どこからどこに, E=増or減, F=商品, G=JAN, H=色サイズ, ...数量列
        # 数量は右の列にあるはず
        # まずヘッダーを確認
        if not all_values:
            return stock

        # ヘッダー行を特定
        header = all_values[0] if all_values else []

        # 数量列のインデックスを探す
        qty_col = None
        for i, h in enumerate(header):
            h_lower = str(h).lower()
            if "数量" in h or "個数" in h or "数" in h and i > 6:
                qty_col = i
                break

        # 数量列が見つからない場合、右端から探す
        if qty_col is None:
            # デフォルト: H列以降で数値が入っている列
            for row in all_values[1:10]:
                for i in range(7, len(row)):
                    if _safe_int(row[i]) > 0:
                        qty_col = i
                        break
                if qty_col is not None:
                    break

        if qty_col is None:
            qty_col = 8  # デフォルト

        COL_INC_DEC = 4  # E列: 増or減
        COL_SKU_AR  = 5  # F列: 商品SKU

        for row in all_values[1:]:
            if len(row) <= max(COL_SKU_AR, COL_INC_DEC, qty_col):
                continue

            sku = str(row[COL_SKU_AR]).strip()
            inc_dec = str(row[COL_INC_DEC]).strip()
            qty = _safe_int(row[qty_col] if len(row) > qty_col else 0)

            if not sku or qty == 0:
                continue

            current = stock.get(sku, 0)
            if "増" in inc_dec:
                stock[sku] = current + qty
            elif "減" in inc_dec:
                stock[sku] = max(0, current - qty)

        print(f"[sheet_reader] 荒瀬倉庫在庫: {len(stock)}件 計算完了", file=sys.stderr)

    except Exception as e:
        import traceback
        print(f"[sheet_reader] 荒瀬倉庫読み込みエラー: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return stock


def load_china_stock(credentials_file: str) -> dict[str, int]:
    """
    在庫発注タイミング＆在庫発注管理表（中国在庫）から仕入れ先在庫を取得（読み取り専用）

    シート構造（各在庫（最新）タブ）:
      - 行1: SKU名（B列以降）
      - 行2: A列="在庫"、B列以降に各SKUの現在庫数

    Returns:
        {sku_code: china_qty}
    """
    stock: dict[str, int] = {}
    try:
        gc = _get_gc(credentials_file)
        ss = gc.open_by_key(CHINA_STOCK_SHEET_ID)
        # タブ名の揺れに対応（スペースあり・なし両方試す）
        tab_candidates = ["各在庫 （最新）", "各在庫（最新）", "各在庫（最新） "]
        ws = None
        for tab in tab_candidates:
            try:
                ws = ss.worksheet(tab)
                break
            except Exception:
                pass
        if ws is None:
            # タイトルに「最新」を含むタブを探す
            for w in ss.worksheets():
                if "最新" in w.title:
                    ws = w
                    break
        if ws is None:
            print("[sheet_reader] 中国在庫: 「各在庫（最新）」タブが見つかりません", file=sys.stderr)
            return stock

        all_values = ws.get_all_values()
        if len(all_values) < 2:
            return stock

        # 行1（index=0）: SKU名ヘッダー
        # 行2（index=1）: 在庫数（A列="在庫" ラベル）
        header_row = all_values[0]
        stock_row = None

        for row in all_values[1:6]:  # 在庫行を探す（最初の5行以内）
            if row and str(row[0]).strip() in ("在庫", "在庫数", "現在庫"):
                stock_row = row
                break

        # "在庫"ラベルが見つからない場合は2行目を使用
        if stock_row is None and len(all_values) >= 2:
            stock_row = all_values[1]

        if stock_row is None:
            return stock

        # SKUコードのパターン（例: MP-01, GC-02, AWC-01, WB-01 など）
        sku_code_pattern = re.compile(r'^[A-Z]{2,4}-\d{2}', re.IGNORECASE)

        # B列（index=1）以降をSKU名と在庫数として対応付け
        for col_idx in range(1, len(header_row)):
            sku_raw = str(header_row[col_idx]).replace('\n', ' ').replace('"', '').strip()
            if not sku_raw:
                continue

            # SKUコードパターンで先頭部分を抽出（例: "MP-01説明書" → "MP-01"）
            m = sku_code_pattern.match(sku_raw)
            if m:
                sku_clean = m.group(0).upper()
            else:
                # パターンに合わない場合は括弧・スペース前まで
                sku_clean = sku_raw.split("（")[0].split("(")[0].split(" ")[0].strip()

            if not sku_clean:
                continue

            qty = _safe_int(stock_row[col_idx] if len(stock_row) > col_idx else 0)
            # 同SKU（MP-01本体・説明書・パッケージ等）は加算せず最初の値を使用
            if sku_clean not in stock:
                stock[sku_clean] = qty

        print(f"[sheet_reader] 中国在庫: {len(stock)}件 読み込み完了", file=sys.stderr)

    except Exception as e:
        import traceback
        print(f"[sheet_reader] 中国在庫読み込みエラー: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return stock


def load_office_stock(credentials_file: str) -> dict[str, int]:
    """LILUCA事務所在庫表から SKU別の現在庫を取得（読み取り専用）

    シート構造（「在庫表専用」タブ）:
      - 行1: 空
      - 行2: ヘッダー（B="SKU", C="商品リスト", D="今在庫"）
      - 行3〜: B列=SKU、C列=商品名、D列=在庫数 （A列は空）

    Returns:
        {sku: current_qty}
    """
    stock: dict[str, int] = {}
    try:
        gc = _get_gc(credentials_file)
        ss = gc.open_by_key(OFFICE_STOCK_SHEET_ID)
        ws = ss.worksheet("在庫表専用")
        all_values = ws.get_all_values()

        if len(all_values) < 3:
            return stock

        # データは行3 (index=2) から、B列=SKU(index=1), D列=qty(index=3)
        for row in all_values[2:]:
            if len(row) < 4:
                continue
            sku = str(row[1]).strip()
            qty = _safe_int(row[3] if len(row) > 3 else 0)
            if not sku:
                continue
            # 同SKUが複数行の場合は加算
            stock[sku] = stock.get(sku, 0) + qty

        print(f"[sheet_reader] 事務所在庫: {len(stock)}件 読み込み完了", file=sys.stderr)

    except Exception as e:
        import traceback
        print(f"[sheet_reader] 事務所在庫読み込みエラー: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return stock


def load_all(credentials_file: str) -> tuple[dict[str, SKUMasterData], dict[str, int], dict[str, int], dict[str, int]]:
    """全データを一括読み込み

    Returns:
        (sku_master, arase_stock, china_stock, office_stock)
        ※ 楽天倉庫在庫は楽天RMS APIから取得するため含まれない
    """
    sku_master = load_sku_master(credentials_file)
    arase_stock = load_arase_stock(credentials_file)
    china_stock = load_china_stock(credentials_file)
    office_stock = load_office_stock(credentials_file)
    return sku_master, arase_stock, china_stock, office_stock
