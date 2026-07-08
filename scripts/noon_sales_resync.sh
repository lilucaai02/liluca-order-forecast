#!/bin/bash
# 昼12時の販売データ再取得（在庫スナップショットは朝5時の値を維持）
# Amazon/楽天の販売実績を過去5日分で再取得し、全11タブに転記する。
set +e
cd /Users/aililuca/amazon

TABS=(
  "マウスピース(在庫)"
  "DS-01 (在庫) "
  "GC-01(在庫)"
  "GC-02(在庫)"
  "TG-01(在庫)"
  "TG-02(在庫)"
  "PCI-01"
  "WB-01(在庫)"
  "WB-02"
  "TS-01"
  "PG-01"
)

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 昼12時 販売再取得 開始 ====="

echo "--- Amazon販売 元シート再取得 ---"
/usr/bin/python3 daily_amazon_sales.py

echo "--- 楽天販売 元シート再取得 ---"
/usr/bin/python3 daily_rakuten_sales.py

echo "--- 全タブ Amazon販売 転記 ---"
for tab in "${TABS[@]}"; do
  /usr/bin/python3 transfer_sales_to_tab.py --tab "$tab"
  sleep 4
done

echo "--- 全タブ 楽天販売 転記 ---"
for tab in "${TABS[@]}"; do
  /usr/bin/python3 transfer_rakuten_sales_to_tab.py --tab "$tab"
  sleep 4
done

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 昼12時 販売再取得 完了 ====="
