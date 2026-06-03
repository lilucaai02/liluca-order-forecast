#!/usr/bin/env python3
"""SKU別 平均販売速度を複数期間まとめて更新してキャッシュに保存.

使い方:
  python3 update_rates.py                    # 7日/30日/90日（推奨・約12分）
  python3 update_rates.py --windows 7,30     # 7日と30日のみ
  python3 update_rates.py --no-rakuten       # Amazonのみ
  python3 update_rates.py --no-amazon        # 楽天のみ

出力:
  data/sku_rates_7d.json
  data/sku_rates_30d.json
  data/sku_rates_90d.json

最長期間1度のAPI取得で全期間分計算するため、複数期間でも所要時間は同じ。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import Settings
from src.sku_rates import (
    fetch_amazon_sku_rates_multi,
    fetch_rakuten_sku_rates_multi,
    save_rates,
    load_cached_rates,
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--windows", default="7,30,90",
                   help="集計期間（カンマ区切り日数、デフォルト 7,30,90）")
    p.add_argument("--no-amazon",  action="store_true")
    p.add_argument("--no-rakuten", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    windows = sorted(set(int(w.strip()) for w in args.windows.split(",") if w.strip()))
    settings = Settings()

    # 既存キャッシュを読み込んでマージ起点とする（部分更新時の上書き防止）
    cache: dict[int, dict[str, float]] = {w: load_cached_rates(w) for w in windows}
    for w in windows:
        print(f"既存キャッシュ {w}日: {len(cache[w])}件")

    if not args.no_amazon:
        print(f"\n=== Amazon SP-API: {windows} レート取得 ===")
        t0 = time.time()
        a = fetch_amazon_sku_rates_multi(settings, windows)
        for w in windows:
            cache[w].update(a[w])
        print(f"  Amazon完了: {len(a[max(windows)])}件 ({time.time()-t0:.0f}秒)")

    if not args.no_rakuten:
        print(f"\n=== 楽天RMS: {windows} レート取得 ===")
        t0 = time.time()
        r = fetch_rakuten_sku_rates_multi(settings, windows)
        for w in windows:
            cache[w].update(r[w])
        print(f"  楽天完了: {len(r[max(windows)])}件 ({time.time()-t0:.0f}秒)")

    print(f"\n✅ 保存完了:")
    for w in windows:
        save_rates(cache[w], meta={"window_days": w}, days=w)
        print(f"  data/sku_rates_{w}d.json: {len(cache[w])} SKU")


if __name__ == "__main__":
    main()
