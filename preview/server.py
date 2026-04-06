#!/usr/bin/env python3
import sys, json, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from http.server import HTTPServer, SimpleHTTPRequestHandler

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(os.path.abspath(__file__)), **kwargs)

    def do_GET(self):
        if self.path == '/data':
            self._serve_data()
        else:
            super().do_GET()

    def _serve_data(self):
        try:
            from config.settings import Settings
            from src.inventory import fetch_inventory, fetch_rakuten_inventory
            from src.sp_client import SPClient
            from src.rakuten_client import RakutenClient
            from src.monitor import load_thresholds
            from src.sale_calendar import SaleCalendar

            from src.sku_rates import load_cached_rates

            settings = Settings()
            defaults, sku_configs = load_thresholds(settings.thresholds_file)
            calendar = SaleCalendar()

            # キャッシュ済みSKU別レートを読み込む（なければアカウント按分で推定）
            cached_rates = load_cached_rates()

            # 在庫取得
            all_items = []
            account_daily_rates = {}  # フォールバック用

            for acc in settings.get_accounts():
                try:
                    client = SPClient(settings, account=acc)
                    items = fetch_inventory(client)
                    all_items.extend(items)
                    if not cached_rates:
                        # キャッシュなし → アカウント合計で推定
                        try:
                            end_dt = datetime.now(timezone.utc)
                            start_dt = end_dt - timedelta(days=30)
                            from datetime import date
                            metrics = client.get_order_metrics(
                                start_dt.strftime("%Y-%m-%dT00:00:00Z"),
                                end_dt.strftime("%Y-%m-%dT00:00:00Z"),
                                granularity="Day"
                            )
                            normal_units = [
                                m.get('unitCount', 0) for m in metrics
                                if m.get('interval', '').split('T')[0]
                                and not calendar.is_sale_day(
                                    date.fromisoformat(m['interval'].split('T')[0])
                                )
                            ]
                            if normal_units:
                                account_daily_rates[acc.name] = sum(normal_units) / len(normal_units)
                        except Exception as e:
                            print(f'[{acc.name}] 販売速度取得エラー: {e}', file=sys.stderr)
                except Exception as e:
                    print(f'[Amazon:{acc.name}] error: {e}', file=sys.stderr)

            for acc in settings.get_rakuten_accounts():
                try:
                    client = RakutenClient(acc)
                    items = fetch_rakuten_inventory(client)
                    all_items.extend(items)
                except Exception as e:
                    print(f'[楽天:{acc.name}] error: {e}', file=sys.stderr)

            # フォールバック用: アカウント別合計在庫
            account_total_qty = {}
            for item in all_items:
                if item.marketplace == 'amazon':
                    account_total_qty[item.account_name] = \
                        account_total_qty.get(item.account_name, 0) + max(item.fulfillable_quantity, 0)

            rows = []
            alert_skus = []
            for item in all_items:
                t = sku_configs.get(item.seller_sku, defaults)
                q = item.fulfillable_quantity
                if q == 0: status = '在庫切れ'
                elif q <= t.critical_level: status = '危険'
                elif q <= t.reorder_point: status = '要発注'
                else: status = '正常'

                # セール考慮の残り日数を計算
                days_left = None
                if q > 0:
                    # 優先1: キャッシュ済み高精度レート
                    sku_daily_rate = cached_rates.get(item.seller_sku)
                    # 優先2: フォールバック（アカウント合計按分）
                    if not sku_daily_rate and item.marketplace == 'amazon':
                        acc_rate = account_daily_rates.get(item.account_name)
                        acc_total = account_total_qty.get(item.account_name, 0)
                        if acc_rate and acc_total > 0:
                            sku_daily_rate = acc_rate * (q / acc_total)
                    if sku_daily_rate and sku_daily_rate > 0:
                        days_left = round(calendar.projected_days_remaining(q, sku_daily_rate))

                # 30日以内アラート
                alert_30 = days_left is not None and days_left < 30

                row = {
                    'account': item.account_name,
                    'marketplace': item.marketplace,
                    'sku': item.seller_sku,
                    'asin': item.asin,
                    'name': item.product_name[:40],
                    'qty': q,
                    'total': item.total_quantity,
                    'reorder': t.reorder_point,
                    'status': status,
                    'days_left': days_left,
                    'alert_30': alert_30,
                }
                rows.append(row)
                if alert_30:
                    alert_skus.append(item.seller_sku)

            total = len(rows)
            data = {
                'rows': rows,
                'stats': {
                    'total': total,
                    'out': sum(1 for r in rows if r['status'] == '在庫切れ'),
                    'critical': sum(1 for r in rows if r['status'] == '危険'),
                    'warning': sum(1 for r in rows if r['status'] == '要発注'),
                    'ok': sum(1 for r in rows if r['status'] == '正常'),
                    'alert_30': len(alert_skus),
                },
                'sale_events': [
                    {'name': e.name, 'start': e.start_date, 'end': e.end_date, 'multiplier': e.multiplier}
                    for e in calendar.list_events()
                ],
            }
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        except Exception as e:
            import traceback
            traceback.print_exc()
            body = json.dumps({'error': str(e), 'rows': [], 'stats': {}}).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3737
    print(f'Preview server: http://localhost:{port}', flush=True)
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
