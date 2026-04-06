from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import Settings
from src.alerts import AlertDispatcher
from src.inventory import fetch_inventory, fetch_rakuten_inventory, save_snapshot
from src.monitor import check_inventory, load_thresholds
from src.rakuten_client import RakutenClient
from src.sales import fetch_sales_velocity
from src.sp_client import SPClient


def _run_check(settings: Settings) -> None:
    """1回分の在庫チェック・アラート送信を全アカウントに対して実行."""
    accounts = settings.get_accounts()
    if not accounts:
        # レガシー: 単一アカウント
        accounts = [None]

    all_items = []
    all_sales: dict = {}

    for account in accounts:
        account_name = account.name if account else "default"
        client = SPClient(settings, account=account)

        try:
            items = fetch_inventory(client)
        except Exception as e:
            print(f"[{account_name}] 在庫取得エラー: {e}")
            continue

        if items:
            all_items.extend(items)

        try:
            sales = fetch_sales_velocity(client)
            all_sales.update(sales)
        except Exception:
            pass

        print(f"[Amazon:{account_name}] {len(items)}件の在庫を取得")

    # 楽天アカウント
    for rakuten_acc in settings.get_rakuten_accounts():
        try:
            rclient = RakutenClient(rakuten_acc)
            items = fetch_rakuten_inventory(rclient)
        except Exception as e:
            print(f"[楽天:{rakuten_acc.name}] 在庫取得エラー: {e}")
            continue
        if items:
            all_items.extend(items)
        print(f"[楽天:{rakuten_acc.name}] {len(items)}件の在庫を取得")

    if not all_items:
        return

    save_snapshot(all_items, settings.data_dir)
    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    alerts = check_inventory(all_items, defaults, sku_configs, all_sales)
    if not alerts:
        print("全アカウント・全SKU正常")
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
