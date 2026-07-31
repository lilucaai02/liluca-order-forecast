#!/usr/bin/env python3
"""ChatWork 通知スクリプト (在庫切れ / 危険 / 要発注)

要発注/危険/在庫切れになった商品×チャネルを ChatWork に通知。
前回スナップショットと比較し、新規に該当したものだけ送信（重複通知防止）。

データ元 (2026-07-31 変更):
  以前はローカルのダッシュボードサーバー (http://localhost:3737/data) を
  叩いていたため、サーバーが落ちていると exit 1 で通知が届かなかった。
  現在はスプレッドシートの「ダッシュボード２」タブを直接読む。
  サーバーの起動は不要で、シートが読めれば必ず通知できる。

判定基準 (旧サーバー版と同じ。config/thresholds.yaml の defaults を使用):
    在庫 0            → 在庫切れ
    在庫 <= critical_level (既定 10)  → 危険
    在庫 <= reorder_point  (既定 50)  → 要発注
    それ以外          → 正常 (通知しない)
  在庫は「そのチャネルが自分で持っている数」で判定する。
    Amazon = FBA在庫数 / 楽天 = RSL在庫数 / Yahoo = ストッククルー在庫数
  シートで「−」等になっている (そのチャネルで扱っていない) 行は対象外。

使い方:
  python3 notify_chatwork.py                     # 状態変化検出 → 新規アラートのみ
  python3 notify_chatwork.py --always            # 新規0件でも必ず1通送る (cron用)
  python3 notify_chatwork.py --force-all         # 現状の全アラートを送信
  python3 notify_chatwork.py --status 要発注      # 特定ステータスのみ
  python3 notify_chatwork.py --dry-run           # 送信せず内容を表示
  python3 notify_chatwork.py --reset             # スナップショットをリセット

cronで毎朝8時に実行する例:
  0 8 * * *  cd /Users/aililuca/amazon && /usr/bin/python3 notify_chatwork.py --always

注意: 本スクリプトは読み取り専用。シートへの書き込みは一切行わない。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

socket.setdefaulttimeout(120)

from dotenv import load_dotenv

# ダッシュボード２の読み方 (ヘッダー名・正規化・数値化) は
# check_sales_anomaly.py と同じものを使う (食い違い防止)。
from check_sales_anomaly import (
    DASHBOARD_TAB,
    DEST_SPREADSHEET_ID,
    DH_FBA_STOCK,
    DH_RSL_STOCK,
    DH_SC_STOCK,
    DH_DAYS,
    find_col,
    norm_header,
    to_number,
)
from config.settings import Settings
from src.fetch_safety import sheets_retry

SNAPSHOT_FILE = Path(__file__).parent / "data" / "chatwork_snapshot.json"
DEFAULT_STATUSES = ["要発注", "危険", "在庫切れ"]

# 残り日数の列 (FBA残り日数は check_sales_anomaly の DH_DAYS を使う)
DH_RSL_DAYS = "RSL残り日数"
DH_SC_DAYS = "Stock Crew残り日数"

# (プラットフォームキー, 表示名, 在庫数の列見出し, 残り日数の列見出し)
PLATFORMS = (
    ("amazon", "Amazon", DH_FBA_STOCK, DH_DAYS),
    ("rakuten", "楽天", DH_RSL_STOCK, DH_RSL_DAYS),
    ("yahoo", "Yahoo", DH_SC_STOCK, DH_SC_DAYS),
)

# ステータスごとに本文へ載せる上限 (旧サーバー版と同じ)
MAX_PER_STATUS = 15


def open_dashboard(settings: Settings):
    """ダッシュボード２のワークシートを開く (読み取り専用)。"""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        settings.google_credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sp = sheets_retry(gc.open_by_key, DEST_SPREADSHEET_ID)
    return sheets_retry(sp.worksheet, DASHBOARD_TAB)


def fetch_dashboard_rows(settings: Settings) -> tuple[List[dict], str]:
    """ダッシュボード２から「商品×チャネル」の行を組み立てる。

    旧サーバー版の /data と同じ形 (sku / platform / qty / status / …) を返す。
    列位置は必ず1行目の見出しから解決する (決め打ちしない)。
    """
    ws = open_dashboard(settings)
    grid = sheets_retry(ws.get, f"A1:BZ{ws.row_count}",
                        value_render_option="UNFORMATTED_VALUE")
    if not grid:
        raise RuntimeError(f"{DASHBOARD_TAB} の中身が読めません")

    header = [norm_header(h) for h in grid[0]]
    idx: Dict[str, Optional[int]] = {}
    for _key, _name, qty_h, days_h in PLATFORMS:
        for name in (qty_h, days_h):
            i = find_col(header, name)
            if i is None:
                raise RuntimeError(
                    f"{DASHBOARD_TAB} に見出し '{name}' が見つかりません")
            idx[name] = i

    # 判定に使う閾値 (旧サーバー版と同じ config/thresholds.yaml の defaults)
    from src.monitor import load_thresholds
    defaults, _sku_configs = load_thresholds(settings.thresholds_file)

    rows: List[dict] = []
    for row in grid[1:]:
        if not row:
            continue
        name = str(row[0]).strip()
        if not name:
            continue

        def cell(col_name: str):
            i = idx[col_name]
            return row[i] if i is not None and i < len(row) else ""

        for key, disp, qty_h, days_h in PLATFORMS:
            qty = to_number(cell(qty_h))
            if qty is None:      # 「−」「空欄」= そのチャネルでは扱っていない
                continue
            days_left = to_number(cell(days_h))
            if qty == 0:
                status = "在庫切れ"
            elif qty <= defaults.critical_level:
                status = "危険"
            elif qty <= defaults.reorder_point:
                status = "要発注"
            else:
                status = "正常"
            rate = (round(qty / days_left, 2)
                    if days_left and days_left > 0 else None)
            rows.append({
                "sku": name,
                "platform": key,
                "platform_name": disp,
                "qty": int(qty),
                "days_left": int(days_left) if days_left is not None else None,
                "rate": rate,
                "status": status,
                "active": True,
            })

    url = (f"https://docs.google.com/spreadsheets/d/{DEST_SPREADSHEET_ID}"
           f"/edit#gid={ws.id}")
    return rows, url


def post_to_chatwork(token: str, room_id: str, body: str) -> dict:
    """ChatWork API でメッセージ送信."""
    url = f"https://api.chatwork.com/v2/rooms/{room_id}/messages"
    data = urllib.parse.urlencode({"body": body, "self_unread": "1"}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "X-ChatWorkToken": token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ChatWork API エラー [{e.code}]: {body_err}") from e


def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return {"alerts": []}


def save_snapshot(alerts: list, ts: str) -> None:
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(
        {"updated_at": ts, "alerts": alerts},
        ensure_ascii=False, indent=2,
    ), encoding="utf-8")


def alert_key(row: dict) -> str:
    """アラートユニークキー（SKU + プラットフォーム + ステータス）."""
    return f"{row.get('platform','')}::{row.get('sku','')}::{row.get('status','')}"


def format_message(new_alerts: list, all_alerts: list, force_all: bool,
                   url: str) -> str:
    """ChatWork メッセージ本文を生成."""
    lines = []
    lines.append("[info][title]📦 在庫アラート通知[/title]")

    if force_all:
        lines.append(f"現在の要対応SKU: 計 {len(all_alerts)} 件")
    else:
        lines.append(f"🆕 新規アラート: {len(new_alerts)} 件 / 全 {len(all_alerts)} 件")

    targets = all_alerts if force_all else new_alerts
    if not targets:
        lines.append("\n新規アラートなし（前回チェックから状態変化なし）")
    else:
        # ステータス別にグループ化
        by_status: dict = {}
        for a in targets:
            by_status.setdefault(a.get("status", "?"), []).append(a)

        # 重要度順
        order = ["在庫切れ", "危険", "要発注"]
        for st in order + [s for s in by_status if s not in order]:
            items = by_status.get(st)
            if not items:
                continue
            items.sort(key=lambda a: (a.get("days_left") if a.get("days_left")
                                      is not None else 99999, a.get("sku", "")))
            icon = {"在庫切れ": "🔴", "危険": "🟠", "要発注": "🟡"}.get(st, "⚪")
            lines.append(f"\n{icon} [b]{st}[/b] ({len(items)}件)")
            # SKU一覧（最大15件）
            for a in items[:MAX_PER_STATUS]:
                sku = a.get("sku", "")
                qty = a.get("qty", 0)
                rate = a.get("rate")
                dl = a.get("days_left")
                mp = a.get("platform_name") or a.get("platform", "")
                rate_str = f", 日販{rate}個/日" if rate else ""
                dl_str = f", 残り{dl}日" if dl is not None else ""
                lines.append(f"  ・[{mp}] {sku}: 在庫{qty}{rate_str}{dl_str}")
            if len(items) > MAX_PER_STATUS:
                lines.append(f"  ...他 {len(items)-MAX_PER_STATUS} 件")

    lines.append("\n[hr]")
    lines.append(f"📊 ダッシュボード: {url}")
    lines.append(f"⏰ {datetime.now().strftime('%Y/%m/%d %H:%M')}")
    lines.append("[/info]")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force-all", action="store_true",
                   help="状態変化に関わらず現状の全アラートを送信")
    p.add_argument("--always", action="store_true",
                   help="新規アラートが0件でも1通送る (毎朝必ず届かせたいとき)")
    p.add_argument("--status", action="append",
                   help="通知対象ステータス（複数可、デフォルト: 要発注/危険/在庫切れ）")
    p.add_argument("--dry-run", action="store_true",
                   help="実際には送信せずメッセージ内容を表示")
    p.add_argument("--reset", action="store_true",
                   help="スナップショットをリセットして終了")
    args = p.parse_args()

    load_dotenv()
    settings = Settings()

    if args.reset:
        if SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.unlink()
            print(f"✅ スナップショット削除: {SNAPSHOT_FILE}")
        else:
            print("スナップショット無し")
        return 0

    token = settings.chatwork_api_token or os.environ.get("CHATWORK_API_TOKEN", "")
    room_id = (settings.chatwork_room_id_stock or settings.chatwork_room_id
               or os.environ.get("CHATWORK_ROOM_ID", ""))
    if not args.dry_run:
        if not token or not room_id:
            print("ERROR: .env に CHATWORK_API_TOKEN / CHATWORK_ROOM_ID を設定してください",
                  file=sys.stderr)
            return 1

    if not settings.google_credentials_file:
        print("ERROR: .env の GOOGLE_CREDENTIALS_FILE を確認してください",
              file=sys.stderr)
        return 1

    statuses = args.status or DEFAULT_STATUSES

    print(f"📥 {DASHBOARD_TAB} 読み取り中...")
    try:
        rows, url = fetch_dashboard_rows(settings)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {DASHBOARD_TAB} を読めません: {e}", file=sys.stderr)
        return 1
    print(f"  → {len(rows)}行 (商品×チャネル)")

    # 対象アラート抽出
    all_alerts = [r for r in rows if r.get("status") in statuses and r.get("active", True)]
    print(f"⚠️  対象アラート: {len(all_alerts)}件 (ステータス: {','.join(statuses)})")

    snapshot = load_snapshot()
    prev_keys = set(snapshot.get("alerts", []))
    cur_keys = {alert_key(a) for a in all_alerts}
    new_keys = cur_keys - prev_keys
    new_alerts = [a for a in all_alerts if alert_key(a) in new_keys]
    print(f"🆕 新規アラート: {len(new_alerts)}件")

    body = format_message(new_alerts, all_alerts, force_all=args.force_all,
                          url=url)

    if args.dry_run:
        print("\n========== ChatWork メッセージ (dry-run) ==========")
        print(body)
        print("====================================================")
        return 0

    if not args.force_all and not args.always and not new_alerts:
        print("✅ 新規アラート無し → ChatWork送信スキップ")
        # スナップショットだけ更新
        save_snapshot(sorted(cur_keys), datetime.now().isoformat())
        return 0

    print(f"📤 ChatWork ルーム {room_id} に送信中...")
    result = post_to_chatwork(token, room_id, body)
    print(f"✅ 送信成功: message_id={result.get('message_id', '?')}")

    # スナップショット更新
    save_snapshot(sorted(cur_keys), datetime.now().isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
