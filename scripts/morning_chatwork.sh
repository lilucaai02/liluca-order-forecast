#!/bin/bash
# ChatWork 朝の在庫通知 (cron 用ラッパー)
#
# 動作:
#   1. ダッシュボードサーバー (localhost:3737) の生存確認
#   2. 落ちていれば自動起動 → データ初期化を90秒待機
#   3. notify_chatwork.py で要発注/危険/在庫切れSKUを通知
#   4. 自分で起動した場合は終了時にサーバー停止
#
# cron 設定例 (毎朝8時):
#   0 8 * * * /bin/bash /Users/aililuca/amazon/scripts/morning_chatwork.sh

set -u

cd /Users/aililuca/amazon || exit 1

LOG=/tmp/chatwork.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') ChatWork通知ジョブ開始 =====" >> "$LOG"

AUTO_STARTED=0
SERVER_PID=""

# サーバー起動チェック
if ! /usr/bin/curl -s -m 5 -o /dev/null http://localhost:3737/ ; then
    echo "[$(date '+%H:%M:%S')] サーバー未起動 → 自動起動中" >> "$LOG"
    /usr/bin/python3 preview/server.py 3737 >> /tmp/preview_server.log 2>&1 &
    SERVER_PID=$!
    AUTO_STARTED=1
    # データ初期化（最初の /data リクエストで4 sheets+各APIをロード）を待機
    sleep 90
fi

# 通知送信
/usr/bin/python3 notify_chatwork.py >> "$LOG" 2>&1
RC=$?
echo "[$(date '+%H:%M:%S')] notify_chatwork.py 終了コード=$RC" >> "$LOG"

# 自動起動した場合は終了
if [ "$AUTO_STARTED" = "1" ] && [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null
    echo "[$(date '+%H:%M:%S')] 自動起動サーバーを停止" >> "$LOG"
fi

echo "===== ジョブ終了 =====" >> "$LOG"
exit $RC
