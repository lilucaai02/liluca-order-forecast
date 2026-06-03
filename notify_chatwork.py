#!/usr/bin/env python3
"""ChatWork 通知スクリプト

要発注/危険/在庫切れ/LT発注アラートになったSKUをChatWorkに通知。
前回スナップショットと比較し、新規に該当したSKUのみ送信（重複通知防止）。

使い方:
  python3 notify_chatwork.py                     # 状態変化検出 → 新規アラートのみ
  python3 notify_chatwork.py --force-all         # 現状の全アラートを送信
  python3 notify_chatwork.py --status 要発注      # 特定ステータスのみ
  python3 notify_chatwork.py --dry-run           # 送信せず内容を表示
  python3 notify_chatwork.py --reset             # スナップショットをリセット

cronで毎朝8時に実行する例:
  0 8 * * *  cd /Users/aililuca/amazon && /usr/bin/python3 notify_chatwork.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

SNAPSHOT_FILE = Path("data/chatwork_snapshot.json")
DEFAULT_STATUSES = ["要発注", "危険", "在庫切れ"]


def fetch_dashboard_data() -> dict:
    """プレビューサーバーからダッシュボードデータを取得.

    サーバー(localhost:3737)が起動していればそこから、無ければ直接生成。
    """
    try:
        with urllib.request.urlopen("http://localhost:3737/data", timeout=180) as r:
            return json.loads(r.read())
    except Exception:
        # サーバー未起動 → 直接サーバーロジックを呼ぶのは大変なのでエラー
        print("ERROR: ダッシュボードサーバーが起動していません。", file=sys.stderr)
        print("先に `python3 preview/server.py 3737 &` を実行してください。", file=sys.stderr)
        sys.exit(1)


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


def format_message(new_alerts: list, all_alerts: list, force_all: bool) -> str:
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
            icon = {"在庫切れ": "🔴", "危険": "🟠", "要発注": "🟡"}.get(st, "⚪")
            lines.append(f"\n{icon} [b]{st}[/b] ({len(items)}件)")
            # SKU一覧（最大15件）
            for a in items[:15]:
                sku = a.get("sku", "")
                qty = a.get("qty", 0)
                rate7 = a.get("rate_7d")
                dl = a.get("days_left_7d") or a.get("days_left")
                mp = {"amazon": "Amazon", "rakuten": "楽天", "yahoo": "Yahoo"}.get(
                    a.get("platform", ""), a.get("platform", ""))
                accs = a.get("accounts") or []
                acc_str = f"({','.join(accs)})" if accs else ""
                rate_str = f", 7日={rate7}個/日" if rate7 else ""
                dl_str = f", 残り{dl}日" if dl is not None else ""
                lines.append(f"  ・[{mp}{acc_str}] {sku}: 在庫{qty}{rate_str}{dl_str}")
            if len(items) > 15:
                lines.append(f"  ...他 {len(items)-15} 件")

    lines.append("\n[hr]")
    lines.append(f"📊 ダッシュボード: http://localhost:3737/")
    lines.append(f"⏰ {datetime.now().strftime('%Y/%m/%d %H:%M')}")
    lines.append("[/info]")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--force-all", action="store_true",
                   help="状態変化に関わらず現状の全アラートを送信")
    p.add_argument("--status", action="append",
                   help="通知対象ステータス（複数可、デフォルト: 要発注/危険/在庫切れ）")
    p.add_argument("--dry-run", action="store_true",
                   help="実際には送信せずメッセージ内容を表示")
    p.add_argument("--reset", action="store_true",
                   help="スナップショットをリセットして終了")
    args = p.parse_args()

    load_dotenv()

    if args.reset:
        if SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.unlink()
            print(f"✅ スナップショット削除: {SNAPSHOT_FILE}")
        else:
            print("スナップショット無し")
        return

    token = os.environ.get("CHATWORK_API_TOKEN", "")
    room_id = os.environ.get("CHATWORK_ROOM_ID", "")
    if not args.dry_run:
        if not token or not room_id:
            print("ERROR: .env に CHATWORK_API_TOKEN / CHATWORK_ROOM_ID を設定してください")
            sys.exit(1)

    statuses = args.status or DEFAULT_STATUSES

    print("📥 ダッシュボードデータ取得中...")
    data = fetch_dashboard_data()
    rows = data.get("rows", [])
    print(f"  → {len(rows)}行取得")

    # 対象アラート抽出
    all_alerts = [r for r in rows if r.get("status") in statuses and r.get("active", True)]
    print(f"⚠️  対象アラート: {len(all_alerts)}件 (ステータス: {','.join(statuses)})")

    snapshot = load_snapshot()
    prev_keys = set(snapshot.get("alerts", []))
    cur_keys = {alert_key(a) for a in all_alerts}
    new_keys = cur_keys - prev_keys
    new_alerts = [a for a in all_alerts if alert_key(a) in new_keys]
    print(f"🆕 新規アラート: {len(new_alerts)}件")

    body = format_message(new_alerts, all_alerts, force_all=args.force_all)

    if args.dry_run:
        print("\n========== ChatWork メッセージ (dry-run) ==========")
        print(body)
        print("====================================================")
        return

    if not args.force_all and not new_alerts:
        print("✅ 新規アラート無し → ChatWork送信スキップ")
        # スナップショットだけ更新
        save_snapshot(sorted(cur_keys), datetime.now().isoformat())
        return

    print(f"📤 ChatWork ルーム {room_id} に送信中...")
    result = post_to_chatwork(token, room_id, body)
    print(f"✅ 送信成功: message_id={result.get('message_id', '?')}")

    # スナップショット更新
    save_snapshot(sorted(cur_keys), datetime.now().isoformat())


if __name__ == "__main__":
    main()
