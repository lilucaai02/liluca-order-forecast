from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import Settings
from src.alerts import AlertDispatcher
from src.inventory import fetch_inventory, save_snapshot
from src.monitor import check_inventory, load_thresholds
from src.sales import fetch_sales_velocity
from src.sp_client import SPClient


def _run_check(settings: Settings) -> None:
    """1回分の在庫チェック・アラート送信を実行."""
    client = SPClient(settings)

    try:
        items = fetch_inventory(client)
    except Exception as e:
        print(f"在庫取得エラー: {e}")
        return

    if not items:
        return

    save_snapshot(items, settings.data_dir)
    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    sales_data = {}
    try:
        sales_data = fetch_sales_velocity(client)
    except Exception:
        pass

    alerts = check_inventory(items, defaults, sku_configs, sales_data)
    if not alerts:
        print("全SKU正常")
        return

    dispatcher = AlertDispatcher(settings)
    sent = dispatcher.dispatch(alerts)
    print(f"アラート: {len(alerts)}件検出, {len(sent)}件送信")


def start_scheduler(settings: Settings, interval_minutes: int) -> None:
    """定期実行スケジューラを起動."""
    scheduler = BlockingScheduler()
    scheduler.add_job(
        _run_check,
        "interval",
        minutes=interval_minutes,
        args=[settings],
        id="inventory_check",
        name="在庫チェック",
    )
    # 起動時に即座に1回実行
    _run_check(settings)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nスケジューラを停止しました。")
