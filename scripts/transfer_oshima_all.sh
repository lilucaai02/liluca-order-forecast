#!/bin/bash
# 大島コピー (1MzyWa...) への日次転記: Amazon販売 / 楽天販売 / 在庫 (FBA+RSL)
# cron 用。Yahoo販売は refresh_token 取得後に追加予定。
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

for tab in "${TABS[@]}"; do
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') [大島/$tab] Amazon販売転記 ==="
  /usr/bin/python3 /Users/aililuca/amazon/transfer_sales_to_oshima_tab.py --tab "$tab"
  sleep 6
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') [大島/$tab] 楽天販売転記 ==="
  /usr/bin/python3 /Users/aililuca/amazon/transfer_rakuten_sales_to_oshima_tab.py --tab "$tab"
  sleep 6
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') [大島/$tab] 在庫転記 ==="
  /usr/bin/python3 /Users/aililuca/amazon/transfer_inventory_to_oshima_tab.py --tab "$tab"
  sleep 6
done
