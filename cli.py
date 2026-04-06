from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

from config.settings import AccountConfig, RakutenAccountConfig, Settings
from src.alerts import AlertDispatcher
from src.exporters import export_csv, export_excel, export_google_sheets, export_pdf
from src.inventory import fetch_inventory, fetch_rakuten_inventory, save_snapshot
from src.models import AlertLevel, InventoryItem
from src.monitor import check_inventory, load_thresholds
from src.rakuten_client import RakutenClient
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


def _resolve_accounts(settings: Settings, account_name: str | None) -> list[AccountConfig]:
    """--account オプションに基づいてAmazonアカウントリストを返す."""
    accounts = settings.get_accounts()

    if account_name is None or account_name == "all":
        return accounts

    acc = settings.get_account(account_name)
    if acc is None:
        available = ", ".join(a.name for a in accounts)
        console.print(f"[red]エラー: Amazonアカウント '{account_name}' が見つかりません。[/red]")
        console.print(f"  利用可能: {available}")
        sys.exit(1)
    return [acc]


def _resolve_rakuten_accounts(
    settings: Settings, account_name: str | None
) -> list[RakutenAccountConfig]:
    """--account オプションに基づいて楽天アカウントリストを返す."""
    accounts = settings.get_rakuten_accounts()

    if account_name is None or account_name == "all":
        return accounts

    acc = settings.get_rakuten_account(account_name)
    if acc is None:
        return []
    return [acc]


def _fetch_all_items(
    settings: Settings,
    account_name: str | None,
    mp_filter: str,
    sku_list: list[str] | None = None,
) -> list[InventoryItem]:
    """Amazon + 楽天の在庫を取得して統合リストを返す."""
    all_items: list[InventoryItem] = []

    if mp_filter in ("all", "amazon"):
        for acc in _resolve_accounts(settings, account_name):
            client = SPClient(settings, account=acc)
            with console.status(f"[Amazon:{acc.name}] 在庫データを取得中..."):
                items = fetch_inventory(client, skus=sku_list)
            all_items.extend(items)

    if mp_filter in ("all", "rakuten"):
        for racc in _resolve_rakuten_accounts(settings, account_name):
            rclient = RakutenClient(racc)
            with console.status(f"[楽天:{racc.name}] 在庫データを取得中..."):
                items = fetch_rakuten_inventory(rclient)
            all_items.extend(items)

    return all_items


@click.group()
def main():
    """Amazon SP-API 在庫監視・発注アラートツール"""
    pass


@main.command()
def accounts():
    """登録済みアカウント一覧を表示."""
    settings = _get_settings()

    # Amazon アカウント
    amazon_accs = settings.get_accounts()
    if amazon_accs:
        table = Table(title="Amazon アカウント")
        table.add_column("#", justify="right")
        table.add_column("アカウント名", style="cyan")
        table.add_column("REFRESH_TOKEN", max_width=40)
        for i, acc in enumerate(amazon_accs, 1):
            token_preview = acc.refresh_token[:20] + "..." if len(acc.refresh_token) > 20 else acc.refresh_token
            table.add_row(str(i), acc.name, token_preview)
        console.print(table)
    else:
        console.print("[yellow]Amazonアカウントが登録されていません。[/yellow]")

    # 楽天 アカウント
    rakuten_accs = settings.get_rakuten_accounts()
    if rakuten_accs:
        table = Table(title="楽天 アカウント")
        table.add_column("#", justify="right")
        table.add_column("アカウント名", style="cyan")
        table.add_column("認証情報")
        for i, acc in enumerate(rakuten_accs, 1):
            table.add_row(str(i), acc.name, "設定済み")
        console.print(table)
    elif not amazon_accs:
        console.print("[yellow]楽天アカウントも登録されていません。[/yellow]")


@main.command()
@click.option("--skus", default=None, help="対象SKU (カンマ区切り)")
@click.option("--account", "account_name", default=None, help="対象アカウント名 (省略=全アカウント)")
@click.option(
    "--marketplace", "mp_filter",
    type=click.Choice(["all", "amazon", "rakuten"]),
    default="all", help="対象マーケットプレイス",
)
def status(skus: str | None, account_name: str | None, mp_filter: str):
    """現在の在庫状況をテーブル表示."""
    settings = _get_settings()
    sku_list = [s.strip() for s in skus.split(",")] if skus else None
    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    all_items = _fetch_all_items(settings, account_name, mp_filter, sku_list)

    if not all_items:
        console.print("[yellow]在庫データがありません。[/yellow]")
        return

    mp_label = {"amazon": "Amazon", "rakuten": "楽天"}.get(mp_filter, "全MP")
    table = Table(title=f"在庫状況 [{mp_label}]")
    table.add_column("MP", style="dim")
    table.add_column("アカウント", style="dim")
    table.add_column("SKU", style="cyan")
    table.add_column("ASIN/管理番号")
    table.add_column("商品名", max_width=30)
    table.add_column("出荷可能", justify="right")
    table.add_column("予約済", justify="right")
    table.add_column("入庫中", justify="right")
    table.add_column("合計", justify="right")
    table.add_column("発注点", justify="right")
    table.add_column("状態")

    for item in all_items:
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

        mp_name = "Amazon" if item.marketplace == "amazon" else "楽天"
        table.add_row(
            mp_name,
            item.account_name,
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
@click.option("--account", "account_name", default=None, help="対象アカウント名 (省略=全アカウント)")
@click.option(
    "--marketplace", "mp_filter",
    type=click.Choice(["all", "amazon", "rakuten"]),
    default="all", help="対象マーケットプレイス",
)
def check(skus: str | None, dry_run: bool, save: bool, account_name: str | None, mp_filter: str):
    """在庫チェック実行. 閾値以下のSKUに通知."""
    settings = _get_settings()
    sku_list = [s.strip() for s in skus.split(",")] if skus else None
    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    all_items = _fetch_all_items(settings, account_name, mp_filter, sku_list)

    if not all_items:
        console.print("[yellow]在庫データがありません。[/yellow]")
        return

    if save:
        path = save_snapshot(all_items, settings.data_dir)
        console.print(f"スナップショット保存: {path}")

    # 販売速度取得 (Amazon のみ、失敗しても続行)
    sales_data = {}
    if mp_filter in ("all", "amazon"):
        for acc in _resolve_accounts(settings, account_name):
            try:
                client = SPClient(settings, account=acc)
                with console.status(f"[Amazon:{acc.name}] 販売データを取得中..."):
                    sales_data.update(fetch_sales_velocity(client))
            except Exception as e:
                console.print(f"[yellow]販売データ取得失敗 (続行): {e}[/yellow]")

    alerts = check_inventory(all_items, defaults, sku_configs, sales_data)

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
@click.option("--account", "account_name", default=None, help="対象アカウント名 (省略=全アカウント)")
@click.option(
    "--marketplace", "mp_filter",
    type=click.Choice(["all", "amazon", "rakuten"]),
    default="all", help="対象マーケットプレイス",
)
def forecast(skus: str | None, account_name: str | None, mp_filter: str):
    """需要予測と発注推奨を表示."""
    settings = _get_settings()
    sku_list = [s.strip() for s in skus.split(",")] if skus else None
    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    all_items = _fetch_all_items(settings, account_name, mp_filter, sku_list)

    if not all_items:
        console.print("[yellow]在庫データがありません。[/yellow]")
        return

    # 販売速度取得 (Amazon のみ)
    sales_data = {}
    if mp_filter in ("all", "amazon"):
        for acc in _resolve_accounts(settings, account_name):
            try:
                client = SPClient(settings, account=acc)
                with console.status(f"[Amazon:{acc.name}] 販売データを取得中..."):
                    sales_data.update(fetch_sales_velocity(client))
            except Exception as e:
                console.print(f"[yellow]販売データ取得失敗 (続行): {e}[/yellow]")

    from src.forecasting import DemandForecaster

    forecaster = DemandForecaster(settings.data_dir)
    results = forecaster.forecast_all(all_items, defaults, sku_configs, sales_data)

    table = Table(title="発注予測")
    table.add_column("MP", style="dim")
    table.add_column("SKU", style="cyan")
    table.add_column("商品名", max_width=25)
    table.add_column("現在庫", justify="right")
    table.add_column("日次消費", justify="right")
    table.add_column("在庫切れ", justify="right")
    table.add_column("発注点到達", justify="right")
    table.add_column("推奨発注数", justify="right", style="bold")
    table.add_column("発注期限")
    table.add_column("分析方法")

    # SKUからmarketplaceを引くためのマップ
    sku_mp = {item.seller_sku: item.marketplace for item in all_items}

    for r in results:
        stockout = f"{r.days_until_stockout}日" if r.days_until_stockout is not None else "-"
        reorder = f"{r.days_until_reorder_point}日" if r.days_until_reorder_point is not None else "-"
        order_date = r.recommended_order_date or "-"

        if r.recommended_order_qty > 0:
            qty_str = f"[red]{r.recommended_order_qty}個[/red]"
        else:
            qty_str = "[green]不要[/green]"

        mp = sku_mp.get(r.seller_sku, "amazon")
        mp_name = "Amazon" if mp == "amazon" else "楽天"

        table.add_row(
            mp_name,
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
@click.option(
    "--format", "fmt",
    type=click.Choice(["csv", "excel", "pdf", "gsheet"]),
    required=True,
    help="出力形式",
)
@click.option("--output", "-o", default=None, help="出力ファイルパス (gsheet以外)")
@click.option("--skus", default=None, help="対象SKU (カンマ区切り)")
@click.option("--with-forecast", is_flag=True, help="発注予測データも含める")
@click.option("--account", "account_name", default=None, help="対象アカウント名 (省略=全アカウント)")
@click.option(
    "--marketplace", "mp_filter",
    type=click.Choice(["all", "amazon", "rakuten"]),
    default="all", help="対象マーケットプレイス",
)
def export(fmt: str, output: str | None, skus: str | None, with_forecast: bool, account_name: str | None, mp_filter: str):
    """在庫データをCSV / Excel / PDF / Google Sheetsにエクスポート."""
    from datetime import datetime as _dt
    from pathlib import Path

    settings = _get_settings()
    sku_list = [s.strip() for s in skus.split(",")] if skus else None

    # 全マーケットプレイスの在庫を統合
    all_items = _fetch_all_items(settings, account_name, mp_filter, sku_list)
    all_sales_data = {}

    if with_forecast and mp_filter in ("all", "amazon"):
        for acc in _resolve_accounts(settings, account_name):
            try:
                client = SPClient(settings, account=acc)
                with console.status(f"[Amazon:{acc.name}] 販売データを取得中..."):
                    sales = fetch_sales_velocity(client)
                    all_sales_data.update(sales)
            except Exception as e:
                console.print(f"[yellow][{acc.name}] 販売データ取得失敗 (続行): {e}[/yellow]")

    if not all_items:
        console.print("[yellow]在庫データがありません。[/yellow]")
        return

    defaults, sku_configs = load_thresholds(settings.thresholds_file)

    # 予測データ取得 (オプション)
    forecast_results = None
    sales_summary = None
    if with_forecast:
        from src.forecasting import DemandForecaster

        forecaster = DemandForecaster(settings.data_dir)
        forecast_results = forecaster.forecast_all(all_items, defaults, sku_configs, all_sales_data)

        # Sales summary for PDF
        all_vel = all_sales_data.get("ALL")
        if all_vel:
            sales_summary = {
                "avg_daily": all_vel.avg_daily_units,
                "total_units": all_vel.total_units_sold,
            }

    timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        filepath = Path(output or f"data/exports/inventory_{timestamp}.csv")
        result = export_csv(all_items, defaults, sku_configs, filepath, forecast_results=forecast_results)
        console.print(f"[green]CSV出力完了:[/green] {result}")

    elif fmt == "excel":
        filepath = Path(output or f"data/exports/inventory_{timestamp}.xlsx")
        result = export_excel(all_items, defaults, sku_configs, filepath, forecast_results=forecast_results)
        console.print(f"[green]Excel出力完了:[/green] {result}")

    elif fmt == "pdf":
        filepath = Path(output or f"data/exports/inventory_report_{timestamp}.pdf")
        result = export_pdf(
            all_items, defaults, sku_configs, filepath,
            forecast_results=forecast_results,
            sales_summary=sales_summary,
        )
        console.print(f"[green]PDFレポート出力完了:[/green] {result}")

    elif fmt == "gsheet":
        if not settings.has_gsheet_config:
            console.print(
                "[red]Google Sheets設定が未構成です。[/red]\n"
                "  .envに以下を設定してください:\n"
                "  GOOGLE_CREDENTIALS_FILE=path/to/service-account.json\n"
                "  GOOGLE_SPREADSHEET_ID=your-spreadsheet-id"
            )
            return
        with console.status("Google Sheetsに書き込み中..."):
            url = export_google_sheets(
                all_items, defaults, sku_configs,
                spreadsheet_id=settings.google_spreadsheet_id,
                credentials_file=settings.google_credentials_file,
                forecast_results=forecast_results,
            )
        console.print(f"[green]Google Sheets更新完了:[/green] {url}")


@main.command("update-rates")
@click.option("--fast", is_flag=True, default=False, help="高速モード（在庫按分、精度低）")
def update_rates(fast: bool):
    """SKU別日次消費量を更新して thresholds.yaml を再計算."""
    import yaml
    from datetime import date
    from src.sku_rates import update_all_rates, build_thresholds_from_rates
    from src.sale_calendar import SaleCalendar

    settings = _get_settings()
    use_asin = not fast

    if use_asin:
        console.print("[yellow]SKU別レートを高精度取得中（数分かかります）...[/yellow]")
    else:
        console.print("[yellow]SKU別レートを高速推定中...[/yellow]")

    rates = update_all_rates(settings, use_asin_api=use_asin)

    # セール補正
    calendar = SaleCalendar()
    sale_buffer = max(
        calendar.get_multiplier(date.today() + __import__("datetime").timedelta(days=i))
        for i in range(30)
    )
    skus_config = build_thresholds_from_rates(rates, sale_buffer=sale_buffer)

    threshold_path = settings.thresholds_file
    existing = yaml.safe_load(threshold_path.read_text(encoding="utf-8")) or {}
    existing["skus"] = skus_config
    threshold_path.write_text(
        yaml.dump(existing, allow_unicode=True, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )

    console.print(f"[green]完了: {len(rates)} SKUのレートを更新[/green]")
    console.print(f"[green]thresholds.yaml を {len(skus_config)} SKU分更新[/green]")

    # 上位10件を表示
    table = Table(title="SKU別日次消費量 上位10件")
    table.add_column("SKU"); table.add_column("消費量/日", justify="right"); table.add_column("発注点", justify="right")
    for sku, rate in sorted(rates.items(), key=lambda x: -x[1])[:10]:
        cfg = skus_config.get(sku, {})
        table.add_row(sku, f"{rate:.2f}", str(cfg.get("reorder_point", "-")))
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
