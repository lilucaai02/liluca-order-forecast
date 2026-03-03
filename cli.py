from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

from config.settings import Settings
from src.alerts import AlertDispatcher
from src.inventory import fetch_inventory, save_snapshot
from src.models import AlertLevel
from src.monitor import check_inventory, load_thresholds
from src.sales import fetch_sales_velocity
from src.sp_client import SPClient

console = Console()


def _get_settings() -> Settings:
    settings = Settings()
    missing = settings.validate_credentials()
    if missing:
        console.print(
            f"[red]エラー: 以下の認証情報が.envに未設定です: {', '.join(missing)}[/red]"
        )
        console.print("  .env.example を参考に .env ファイルを作成してください。")
        sys.exit(1)
    return settings


@click.group()
def main():
    """Amazon SP-API 在庫監視・発注アラートツール"""
    pass


@main.command()
@click.option("--skus", default=None, help="対象SKU (カンマ区切り)")
def status(skus: str | None):
    """現在の在庫状況をテーブル表示."""
    settings = _get_settings()
    client = SPClient(settings)
    sku_list = [s.strip() for s in skus.split(",")] if skus else None

    with console.status("在庫データを取得中..."):
        items = fetch_inventory(client, skus=sku_list)

    if not items:
        console.print("[yellow]在庫データがありません。[/yellow]")
        return

    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    table = Table(title="FBA在庫状況")
    table.add_column("SKU", style="cyan")
    table.add_column("ASIN")
    table.add_column("商品名", max_width=30)
    table.add_column("出荷可能", justify="right")
    table.add_column("予約済", justify="right")
    table.add_column("入庫中", justify="right")
    table.add_column("合計", justify="right")
    table.add_column("発注点", justify="right")
    table.add_column("状態")

    for item in items:
        threshold = sku_configs.get(item.seller_sku, defaults)
        qty = item.fulfillable_quantity

        if qty == 0:
            state = "[red bold]在庫切れ[/red bold]"
        elif qty <= threshold.critical_level:
            state = "[red]危険[/red]"
        elif qty <= threshold.reorder_point:
            state = "[yellow]要発注[/yellow]"
        else:
            state = "[green]正常[/green]"

        table.add_row(
            item.seller_sku,
            item.asin,
            item.product_name[:30],
            str(qty),
            str(item.reserved_quantity),
            str(item.inbound_shipped_quantity + item.inbound_receiving_quantity),
            str(item.total_quantity),
            str(threshold.reorder_point),
            state,
        )

    console.print(table)


@main.command()
@click.option("--skus", default=None, help="対象SKU (カンマ区切り)")
@click.option("--dry-run", is_flag=True, help="通知を送信せずにチェックのみ")
@click.option("--save", is_flag=True, help="在庫スナップショットを保存")
def check(skus: str | None, dry_run: bool, save: bool):
    """在庫チェック実行. 閾値以下のSKUに通知."""
    settings = _get_settings()
    client = SPClient(settings)
    sku_list = [s.strip() for s in skus.split(",")] if skus else None

    with console.status("在庫データを取得中..."):
        items = fetch_inventory(client, skus=sku_list)

    if not items:
        console.print("[yellow]在庫データがありません。[/yellow]")
        return

    if save:
        path = save_snapshot(items, settings.data_dir)
        console.print(f"スナップショット保存: {path}")

    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    # 販売速度取得 (失敗しても続行)
    sales_data = {}
    try:
        with console.status("販売データを取得中..."):
            sales_data = fetch_sales_velocity(client)
    except Exception as e:
        console.print(f"[yellow]販売データ取得失敗 (続行): {e}[/yellow]")

    alerts = check_inventory(items, defaults, sku_configs, sales_data)

    if not alerts:
        console.print("[green]全SKUの在庫レベルは正常です。[/green]")
        return

    # アラート表示
    level_style = {
        AlertLevel.OUT_OF_STOCK: "red bold",
        AlertLevel.CRITICAL: "red",
        AlertLevel.WARNING: "yellow",
    }
    for alert in alerts:
        console.print(f"[{level_style[alert.level]}]{alert.message}[/{level_style[alert.level]}]")

    if dry_run:
        console.print(f"\n[dim](dry-run: {len(alerts)}件のアラートが検出されました)[/dim]")
        return

    # 通知送信
    if not (settings.has_email_config or settings.has_slack_config):
        console.print("[yellow]通知先が未設定です (.envでSMTPまたはSlackを設定してください)[/yellow]")
        return

    dispatcher = AlertDispatcher(settings)
    sent = dispatcher.dispatch(alerts)
    console.print(f"\n{len(sent)}件の通知を送信しました。")
    if len(sent) < len(alerts):
        console.print(
            f"[dim]({len(alerts) - len(sent)}件はクールダウン中のためスキップ)[/dim]"
        )


@main.command()
@click.option("--skus", default=None, help="対象SKU (カンマ区切り)")
def forecast(skus: str | None):
    """需要予測と発注推奨を表示."""
    settings = _get_settings()
    client = SPClient(settings)
    sku_list = [s.strip() for s in skus.split(",")] if skus else None

    with console.status("在庫データを取得中..."):
        items = fetch_inventory(client, skus=sku_list)

    if not items:
        console.print("[yellow]在庫データがありません。[/yellow]")
        return

    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    sales_data = {}
    try:
        with console.status("販売データを取得中..."):
            sales_data = fetch_sales_velocity(client)
    except Exception as e:
        console.print(f"[yellow]販売データ取得失敗 (続行): {e}[/yellow]")

    from src.forecasting import DemandForecaster

    forecaster = DemandForecaster(settings.data_dir)
    results = forecaster.forecast_all(items, defaults, sku_configs, sales_data)

    table = Table(title="発注予測")
    table.add_column("SKU", style="cyan")
    table.add_column("商品名", max_width=25)
    table.add_column("現在庫", justify="right")
    table.add_column("日次消費", justify="right")
    table.add_column("在庫切れ", justify="right")
    table.add_column("発注点到達", justify="right")
    table.add_column("推奨発注数", justify="right", style="bold")
    table.add_column("発注期限")
    table.add_column("分析方法")

    for r in results:
        stockout = f"{r.days_until_stockout}日" if r.days_until_stockout is not None else "-"
        reorder = f"{r.days_until_reorder_point}日" if r.days_until_reorder_point is not None else "-"
        order_date = r.recommended_order_date or "-"

        if r.recommended_order_qty > 0:
            qty_str = f"[red]{r.recommended_order_qty}個[/red]"
        else:
            qty_str = "[green]不要[/green]"

        table.add_row(
            r.seller_sku,
            r.product_name[:25],
            str(r.current_stock),
            f"{r.daily_consumption_rate:.1f}",
            stockout,
            reorder,
            qty_str,
            order_date,
            r.method,
        )

    console.print(table)


@main.command()
@click.option("--interval", default=None, type=int, help="チェック間隔 (分)")
def run(interval: int | None):
    """定期的に在庫チェックを実行."""
    from scheduler import start_scheduler

    settings = _get_settings()
    minutes = interval or settings.check_interval_minutes
    console.print(f"在庫監視を開始します (間隔: {minutes}分)")
    console.print("Ctrl+C で停止")
    start_scheduler(settings, minutes)


if __name__ == "__main__":
    main()
