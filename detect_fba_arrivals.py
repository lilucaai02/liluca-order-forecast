#!/usr/bin/env python3
"""FBAへの入庫を Amazon の「入荷中」データから自動検知して一覧に記入する。

■ 背景
イーウーパスポート(中国倉庫)から FBA へ送った商品は、Amazon 側で分納で受領される
(例: 500個送って 100→200→200 と入庫)。人が到着を記録するのは手間がかかり、
まとめて記録すると在庫が実態とずれる。

Amazon は SP-API で「いま何個が入荷作業中か」を返してくれる:
    inboundWorkingQuantity + inboundShippedQuantity + inboundReceivingQuantity
これを使えば到着を完全自動で検知できる。実測で、一覧の輸送中残高と完全一致した。

■ 方式 (残高照合方式)
スナップショットの差分ではなく、そのつど残高を突き合わせる。初回から使えて、
取りこぼしても次回に自己修復する。

    受領済み数 = 一覧の未到着残高 − Amazon の入荷中残高

    未到着残高 = 移動先が FBA の行のうち、K=TRUE(出発済み) かつ P=FALSE(未到着) の
                 (個数 − 既に記入済みの到着個数) の合計

受領済み数を、発送日の古い行から順に (FIFO) 消し込み、N列(今到着個数)と
O列(最後到着日付)に書く。全量到着したら I列を「到着」に変える。

商品タブへの転記は行わない。N/O を書くところまでが本スクリプトの仕事で、
そのあと transfer_movement_log.py が「輸送中 → FBA」を商品タブへ書き、P列に
チェックを入れる。役割を分けてあるので二重計上が起きない。

■ 安全装置
- ABSOLUTE_MIN_ROW より前の行は触らない (人手で転記済みのため)
- Amazon の入荷中が一覧の未到着残高を上回る場合は「記録漏れの発送がある」ため
  書き込まず警告だけ出す
- N列は常に累計で上書きする (増やす方向のみ。減る方向には書かない)
- --dry-run で内容だけ確認できる

使い方:
  python3 detect_fba_arrivals.py --dry-run
  python3 detect_fba_arrivals.py
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
from src.fetch_safety import (                          # noqa: E402
    retry_call, sheets_retry, set_default_socket_timeout,
)
from src.inventory import fetch_inventory               # noqa: E402
from src.sp_client import SPClient                      # noqa: E402

SPREADSHEET_ID = "1MzyWaqefWZvHcR4nHSrfzgwXaBA4oe-E9MlfhNjZTAU"
LIST_SHEET = "発注と在庫移動一覧"
SERIAL_BASE = datetime.date(1899, 12, 30)

# 951行目以前は人手で転記済み。触らない。
ABSOLUTE_MIN_ROW = 952

LOCK_PATH = os.path.join(tempfile.gettempdir(), "detect_fba_arrivals.lock")

# 一覧の列 (1始まり)
COL_DATE, COL_SKU, COL_SRC, COL_DST, COL_QTY = 1, 2, 3, 4, 5
COL_STATE, COL_K, COL_ARV_QTY, COL_ARV_DATE, COL_P = 9, 11, 14, 15, 16

STATE_ARRIVED = "到着"

# 一覧の移動先が FBA を指す表記
FBA_DEST_TOKENS = {"fba"}

SKU_ALIAS_RAW = {
    "MP-02MHD4": "MP-02MHD",
    "PCI-01gray": "PCI-01gray1",
    "PG-01m": "pg-01ml",
    "PG-01l": "pg-01xl",
    "WB-01s": "S",
    "WB-01l": "L",
    "TS-01": "ts-01mw",
}


def norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).strip().lower()


SKU_ALIAS = {norm(k): norm(v) for k, v in SKU_ALIAS_RAW.items()}


def col_letter(n: int) -> str:
    r = ""
    while n > 0:
        n, x = divmod(n - 1, 26)
        r = chr(65 + x) + r
    return r


def serial_to_date(v: Any) -> Optional[datetime.date]:
    try:
        return SERIAL_BASE + datetime.timedelta(days=int(v))
    except (TypeError, ValueError):
        return None


def date_to_serial(d: datetime.date) -> int:
    return (d - SERIAL_BASE).days


def is_blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def build_asin_index() -> Dict[str, str]:
    """ASIN → ブロックコード(norm済み)。"""
    out: Dict[str, str] = {}
    for blocks in OSHIMA_TAB_BLOCKS.values():
        for b in blocks:
            asin = str(b.get("asin", "")).strip()
            if asin:
                out.setdefault(asin, norm(b["code"]))
    return out


def build_code_set() -> set:
    return {norm(b["code"]) for blocks in OSHIMA_TAB_BLOCKS.values() for b in blocks}


def resolve_code(sku: Any, codes: set) -> Optional[str]:
    n = norm(sku)
    if n in codes:
        return n
    a = SKU_ALIAS.get(n)
    if a and a in codes:
        return a
    return None


def fetch_amazon_inbound(settings: Settings) -> Tuple[Dict[str, int], List[str]]:
    """ASIN → 入荷中合計。失敗したアカウントは warn に積んで除外する。"""
    inbound: Dict[str, int] = {}
    warn: List[str] = []
    ok = 0
    for acc in settings.get_accounts():
        try:
            client = SPClient(settings, account=acc)
            items = retry_call(lambda: fetch_inventory(client),
                               f"[Amazon:{acc.name}] FBA在庫")
        except Exception as e:  # noqa: BLE001
            warn.append(f"[Amazon:{acc.name}] 在庫取得に失敗したため、この"
                        f"アカウントの商品は判定しません: {e}")
            continue
        ok += 1
        # 同一アカウント内では、同じASINが複数SKUで返ることがある
        # (旧SKUが残っている等)。FBA在庫はASIN単位で管理されるため
        # それらは同じ在庫を指しており、足すと二重計上になる → 最大値を採る。
        # アカウントをまたぐ場合は別々の在庫なので、そのあと合算する。
        per_acc: Dict[str, int] = {}
        for it in items:
            if not it.asin:
                continue
            n = (it.inbound_working_quantity
                 + it.inbound_shipped_quantity
                 + it.inbound_receiving_quantity)
            per_acc[it.asin] = max(per_acc.get(it.asin, 0), n)
        for asin, n in per_acc.items():
            inbound[asin] = inbound.get(asin, 0) + n
    if ok == 0:
        raise RuntimeError("全Amazonアカウントで在庫取得に失敗しました。"
                           "誤検知を防ぐため中止します。")
    return inbound, warn


def acquire_lock():
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("別のプロセスが実行中のため中止します。", file=sys.stderr)
        sys.exit(0)
    return fh


def main() -> None:
    p = argparse.ArgumentParser(description="FBA入庫の自動検知 → 一覧へ記入")
    p.add_argument("--dry-run", action="store_true", help="書き込まず内容だけ表示")
    p.add_argument("--from-row", type=int, default=ABSOLUTE_MIN_ROW,
                   help=f"処理開始行 (既定 {ABSOLUTE_MIN_ROW}。これ未満は拒否)")
    args = p.parse_args()

    if args.from_row < ABSOLUTE_MIN_ROW:
        print(f"エラー: --from-row は {ABSOLUTE_MIN_ROW} 以上にしてください "
              f"(それより前は人手で転記済みのため)", file=sys.stderr)
        sys.exit(1)

    set_default_socket_timeout()
    lock = acquire_lock()  # noqa: F841
    settings = Settings()
    today = datetime.date.today()

    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = sheets_retry(gc.open_by_key, SPREADSHEET_ID)
    ws = sheets_retry(sp.worksheet, LIST_SHEET)

    last_col = col_letter(max(COL_P, COL_ARV_DATE))
    rows = sheets_retry(ws.get, f"A{args.from_row}:{last_col}{ws.row_count}",
                        value_render_option="UNFORMATTED_VALUE")

    codes = build_code_set()
    asin_index = build_asin_index()

    # --- 一覧から「FBA向けで未到着」の行を集める ---------------------------
    warn: List[str] = []
    pending: Dict[str, List[dict]] = {}   # code -> [行の情報] (発送日の古い順)
    for i, raw in enumerate(rows, start=args.from_row):
        r = list(raw) + [""] * (COL_P - len(raw))

        def cell(c: int) -> Any:
            return r[c - 1] if len(r) >= c else ""

        if is_blank(cell(COL_SKU)):
            continue
        if norm(cell(COL_DST)) not in FBA_DEST_TOKENS:
            continue
        if not bool(cell(COL_K)):     # 出発が未転記の行はまだ対象外
            continue
        if bool(cell(COL_P)):         # 到着転記済み
            continue
        code = resolve_code(cell(COL_SKU), codes)
        if code is None:
            warn.append(f"{i}行目: SKU {cell(COL_SKU)!r} に対応する商品が見つかりません")
            continue
        try:
            qty = int(cell(COL_QTY))
        except (TypeError, ValueError):
            warn.append(f"{i}行目: 個数(E列)が数値でないためスキップ")
            continue
        try:
            already = int(cell(COL_ARV_QTY)) if not is_blank(cell(COL_ARV_QTY)) else 0
        except (TypeError, ValueError):
            already = 0
        d = serial_to_date(cell(COL_DATE))
        pending.setdefault(code, []).append({
            "row": i, "sku": cell(COL_SKU), "date": d, "qty": qty,
            "already": already, "remain": max(0, qty - already),
            "state": cell(COL_STATE),
        })
    for lst in pending.values():
        lst.sort(key=lambda x: (x["date"] or datetime.date.min, x["row"]))

    if not pending:
        print("FBA向けの未到着行はありません。")
        return

    # --- Amazon の入荷中を取得 --------------------------------------------
    inbound_by_asin, api_warn = fetch_amazon_inbound(settings)
    warn.extend(api_warn)
    inbound: Dict[str, int] = {}
    for asin, n in inbound_by_asin.items():
        code = asin_index.get(asin)
        if code:
            inbound[code] = inbound.get(code, 0) + n

    # --- 残高照合 → FIFO消し込み ------------------------------------------
    updates: List[dict] = []
    report: List[str] = []
    for code, lst in sorted(pending.items()):
        outstanding = sum(x["remain"] for x in lst)
        if outstanding <= 0:
            continue
        inb = inbound.get(code, 0)
        received = outstanding - inb
        label = lst[0]["sku"]
        if received <= 0:
            report.append(f"  {label:22s} 未到着{outstanding:6d} / 入荷中{inb:6d}"
                          f" → まだ到着なし")
            # 入荷中が記録より多い = 一覧に無い発送があるか、同一ASINが複数SKUで
            # 出品されていて二重に数えている。誤検知はしないが到着を検知できない。
            if inb > outstanding:
                warn.append(
                    f"{label}: Amazonの入荷中({inb})が一覧の未到着({outstanding})を"
                    f"上回っています。一覧に記録していない発送があるか、"
                    f"同一商品が複数SKUで出品されている可能性があります。"
                    f"この商品は到着を自動検知できません")
            continue
        if inb == 0 and code not in inbound:
            warn.append(f"{label}: Amazonの在庫データに見つかりません"
                        f"(ASIN未登録の可能性)。判定をスキップします")
            continue
        report.append(f"  {label:22s} 未到着{outstanding:6d} / 入荷中{inb:6d}"
                      f" → {received}個 到着済みと判定")
        left = received
        for row in lst:
            if left <= 0:
                break
            take = min(left, row["remain"])
            if take <= 0:
                continue
            new_total = row["already"] + take
            full = new_total >= row["qty"]
            updates.append({
                "row": row["row"], "sku": row["sku"],
                "arrive_qty": new_total, "arrive_date": today,
                "set_state": full, "ship_qty": row["qty"],
                "added": take, "before": row["already"],
            })
            left -= take

    # --- 表示 --------------------------------------------------------------
    print(f"=== FBA入庫の自動検知 ({today}) ===")
    for line in report:
        print(line)
    if warn:
        print("\n--- 警告 ---")
        for w in warn:
            print(f"  [警告] {w}")
    if not updates:
        print("\n記入する行はありません。")
        return

    print(f"\n--- 一覧への記入 {len(updates)}件 ---")
    for u in updates:
        state = "  状態→到着" if u["set_state"] else ""
        print(f"  {u['row']}行目 {u['sku']:22s} "
              f"N={u['arrive_qty']} (発送{u['ship_qty']} / 今回+{u['added']})"
              f"  O={u['arrive_date']}{state}")

    if args.dry_run:
        print("\n[dry-run] 書き込みは行いませんでした")
        return

    # --- 書き込み (N列・O列・必要なら I列。P列は転記側が立てる) -------------
    data = []
    for u in updates:
        data.append({"range": f"{LIST_SHEET}!{col_letter(COL_ARV_QTY)}{u['row']}",
                     "values": [[u["arrive_qty"]]]})
        data.append({"range": f"{LIST_SHEET}!{col_letter(COL_ARV_DATE)}{u['row']}",
                     "values": [[date_to_serial(u["arrive_date"])]]})
        if u["set_state"]:
            data.append({"range": f"{LIST_SHEET}!{col_letter(COL_STATE)}{u['row']}",
                         "values": [[STATE_ARRIVED]]})
    for i in range(0, len(data), 100):
        sheets_retry(sp.values_batch_update,
                     {"valueInputOption": "USER_ENTERED",
                      "data": [dict(x) for x in data[i:i + 100]]})
    print(f"\n→ 一覧に {len(data)}セル書き込み完了")
    print("   次に transfer_movement_log.py を実行すると商品タブへ転記されます。")


if __name__ == "__main__":
    main()
