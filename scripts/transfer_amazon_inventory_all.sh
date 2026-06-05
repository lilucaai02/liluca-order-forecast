#!/bin/bash
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
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') [$tab] FBA在庫転記 ==="
  /usr/bin/python3 /Users/aililuca/amazon/transfer_inventory_to_tab.py --tab "$tab"
  sleep 4
done
