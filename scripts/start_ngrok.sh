#!/bin/bash
# ngrok 起動スクリプト - ダッシュボードを外部公開（Basic認証付き）
#
# 使い方:
#   ./scripts/start_ngrok.sh         # 起動 + URLを表示
#   ./scripts/start_ngrok.sh stop    # 停止
#
# 動作:
#   1. ダッシュボードサーバーが3737で動いていることを前提
#   2. ngrok HTTPトンネルをBasic認証付きで開く
#   3. 公開URLを表示

cd /Users/aililuca/amazon || exit 1

# .env から Basic認証情報読込
NGROK_USER=$(grep '^NGROK_BASIC_USER=' .env | cut -d= -f2)
NGROK_PASS=$(grep '^NGROK_BASIC_PASS=' .env | cut -d= -f2)
NGROK_BIN=/Users/aililuca/amazon/ngrok

if [ "${1:-}" = "stop" ]; then
    pkill -f "ngrok http" && echo "✅ ngrok停止" || echo "ngrok未起動"
    exit 0
fi

# 既に起動中?
if pgrep -f "ngrok http 3737" > /dev/null; then
    echo "⚠️  ngrok 既に起動中"
else
    echo "🚀 ngrok 起動中..."
    nohup "$NGROK_BIN" http 3737 \
        --basic-auth "${NGROK_USER}:${NGROK_PASS}" \
        --log=/tmp/ngrok.log \
        > /dev/null 2>&1 &
    sleep 3
fi

# 公開URL取得 (ngrok local API)
URL=$(/usr/bin/curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
      | python3 -c "import sys, json; d=json.load(sys.stdin); t=d.get('tunnels',[]); print(t[0]['public_url']) if t else print('')" 2>/dev/null)

cat <<EOF

==================================================
🌐 ダッシュボード公開URL
==================================================
  URL    : ${URL:-（取得失敗・/tmp/ngrok.log を確認）}
  ユーザー: ${NGROK_USER}
  パスワード: ${NGROK_PASS}
==================================================

ローカル管理画面: http://127.0.0.1:4040
停止コマンド   : $0 stop

EOF
