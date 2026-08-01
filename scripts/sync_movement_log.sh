#!/bin/bash
# 在庫移動一覧の同期 (10分おきに実行)
#   1. FBAへの入庫を Amazon の入荷中データから検知し、一覧の N/O 列に記入
#   2. 一覧の未転記行を商品タブへ転記し、K/P 列にチェック
# 新しい行が無ければ何もしないので、空振り時の負荷はほぼゼロ。
cd /Users/aililuca/amazon || exit 1

# TG-01l は発注中と工場入荷が100個合わないため保留中 (原因確認待ち)
# gc-02rainbow-35 の 987行目は手入力済みのため除外
SKIP="--skip-row 953 --skip-row 964 --skip-row 972 --skip-row 980 --skip-row 987"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
/usr/bin/python3 detect_fba_arrivals.py
/usr/bin/python3 transfer_movement_log.py $SKIP
