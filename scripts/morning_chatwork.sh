#!/bin/bash
# ChatWork 朝の在庫通知 (cron 用ラッパー) — 3通目「在庫切れ・危険・要発注」
#
# 動作:
#   notify_chatwork.py を実行し、在庫切れ/危険/要発注の商品×チャネルを通知する。
#
# 2026-07-31 変更:
#   以前はダッシュボードサーバー (localhost:3737) を自動起動して待ってから
#   叩いていたが、起動に失敗すると通知そのものが飛ばなかった
#   (2026-07-31 朝は exit 1 で未送信)。notify_chatwork.py が
#   スプレッドシート「ダッシュボード２」を直接読むようになったため、
#   サーバーの起動・停止処理は不要になり削除した。
#
# cron 設定例 (毎朝8時10分。8時の check_sales_anomaly.py の2通のあと):
#   10 8 * * * /bin/bash /Users/aililuca/amazon/scripts/morning_chatwork.sh

set -u

cd /Users/aililuca/amazon || exit 1

LOG=/tmp/chatwork.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') ChatWork通知ジョブ開始 =====" >> "$LOG"

# --always: 新規アラートが0件の日でも「新規なし」と1通送る (毎朝必ず届かせる)
/usr/bin/python3 notify_chatwork.py --always >> "$LOG" 2>&1
RC=$?
echo "[$(date '+%H:%M:%S')] notify_chatwork.py 終了コード=$RC" >> "$LOG"

echo "===== ジョブ終了 =====" >> "$LOG"
exit $RC
