#!/usr/bin/env python3
import sys, json, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer

# 簡易メモリキャッシュ（TTL 5分）
_DATA_CACHE = {'body': None, 'expires_at': 0}
_CACHE_TTL_SEC = 300
import threading
_DATA_LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(os.path.abspath(__file__)), **kwargs)

    def do_GET(self):
        if self.path == '/data':
            self._serve_data()
        elif self.path == '/data?refresh=1' or self.path.startswith('/data?refresh'):
            with _DATA_LOCK:
                _DATA_CACHE['body'] = None
                _DATA_CACHE['expires_at'] = 0
            self._serve_data()
        else:
            super().do_GET()

    def _serve_data(self):
        # キャッシュチェック
        import time as _time
        now = _time.time()
        with _DATA_LOCK:
            if _DATA_CACHE['body'] and _DATA_CACHE['expires_at'] > now:
                body = _DATA_CACHE['body']
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('X-Cache', 'HIT')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        try:
            from config.settings import Settings
            from src.inventory import fetch_inventory, fetch_rakuten_inventory, fetch_yahoo_inventory
            from src.sp_client import SPClient
            from src.rakuten_client import RakutenClient
            from src.yahoo_client import YahooClient
            from src.monitor import load_thresholds
            from src.sale_calendar import SaleCalendar
            from src.sku_rates import load_cached_rates
            from src.sheet_reader import load_all as load_sheet_data

            settings = Settings()
            defaults, sku_configs = load_thresholds(settings.thresholds_file)
            calendar = SaleCalendar()

            # ── スプレッドシートから読み取り（既存シートは閲覧のみ）──
            sku_master, arase_stock = {}, {}
            china_stock: dict = {}
            office_stock: dict = {}
            if settings.google_credentials_file:
                try:
                    result = load_sheet_data(settings.google_credentials_file)
                    if len(result) == 4:
                        sku_master, arase_stock, china_stock, office_stock = result
                    elif len(result) == 3:
                        sku_master, arase_stock, china_stock = result
                    else:
                        sku_master, arase_stock = result
                except Exception as e:
                    print(f'[sheet_reader] エラー: {e}', file=sys.stderr)

            # ── キャッシュ済みSKU別レート（7日 / 30日 / 90日 / 旧形式）──
            cached_rates    = load_cached_rates()       # 旧形式（後方互換）
            rates_7d        = load_cached_rates(7)
            rates_30d       = load_cached_rates(30)
            rates_90d       = load_cached_rates(90)

            # ── 在庫取得 ──
            all_items = []
            account_daily_rates = {}

            for acc in settings.get_accounts():
                try:
                    client = SPClient(settings, account=acc)
                    items = fetch_inventory(client)
                    all_items.extend(items)
                    if not cached_rates:
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

            for acc in settings.get_yahoo_accounts():
                try:
                    client = YahooClient(
                        account_name=acc.name,
                        client_id=acc.client_id,
                        seller_id=acc.seller_id,
                        client_secret=acc.client_secret,
                        refresh_token=acc.refresh_token,
                    )
                    items = fetch_yahoo_inventory(client)
                    all_items.extend(items)
                except Exception as e:
                    print(f'[Yahoo:{acc.name}] error: {e}', file=sys.stderr)

            # フォールバック用: アカウント別合計在庫
            account_total_qty = {}
            for item in all_items:
                if item.marketplace == 'amazon':
                    account_total_qty[item.account_name] = \
                        account_total_qty.get(item.account_name, 0) + max(item.fulfillable_quantity, 0)

            # ── SKUマッピング: seller_sku → master_key ──
            # 発注予測シートのSKU名はダッシュ区切りで大文字小文字が異なる場合があるので
            # 正規化して突合する
            def normalize_sku(s: str) -> str:
                return s.lower().replace('-', '').replace('_', '').replace(' ', '')

            import re as _re
            def base_sku(s: str) -> str:
                """seller_skuから基本SKUコードを抽出.

                楽天 "tg-01:tg-01l-red" → ":後ろが英字含む" なら after = "tg-01l-red"
                楽天 "awc-01:173"      → ":後ろが数字のみ" なら before = "awc-01"
                楽天 "mp-01:mp-01"     → after = "mp-01"
                Amazon "MP-02MHD(A)"  → "MP-02MHD"
                """
                if ':' in s:
                    before, after = s.split(':', 1)
                    after_clean = after.split('(')[0].split('（')[0].strip()
                    if after_clean and _re.search(r'[a-zA-Z]', after_clean):
                        s = after_clean
                    else:
                        s = before
                s = s.split('(')[0].split('（')[0].strip()
                return s

            sku_master_normalized = {
                normalize_sku(k): v for k, v in sku_master.items()
            }

            # 楽天APIデータから基本SKU別の合計在庫を集計（楽天倉庫在庫として使用）
            rakuten_wh_normalized: dict = {}
            for item in all_items:
                if item.marketplace == 'rakuten':
                    base = normalize_sku(base_sku(item.seller_sku))
                    rakuten_wh_normalized[base] = (
                        rakuten_wh_normalized.get(base, 0) + max(item.fulfillable_quantity, 0)
                    )

            # 中国在庫の正規化辞書
            china_normalized = {
                normalize_sku(k): v for k, v in china_stock.items()
            }

            # 事務所在庫の正規化辞書
            office_normalized = {
                normalize_sku(k): v for k, v in office_stock.items()
            }

            # ── アカウント名一覧（FBA列をアカウント別にする用）──
            amazon_account_names = [acc.name for acc in settings.get_accounts()]

            # ── 荒瀬倉庫・中国在庫の正規化辞書（基本SKUで引けるよう）──
            arase_normalized: dict = {}
            for k, v in arase_stock.items():
                arase_normalized[normalize_sku(k)] = arase_normalized.get(normalize_sku(k), 0) + v
                # 基本SKU部分も格納（"mp-01-001" → "mp01" にも検索ヒットさせるため）
                bk = normalize_sku(base_sku(k))
                if bk != normalize_sku(k):
                    arase_normalized.setdefault(bk, v)

            # ── 基本SKU単位でアイテムをグルーピング ──
            sku_groups: dict = {}
            for item in all_items:
                bs = base_sku(item.seller_sku)        # 表示用（元ケース保持）
                bk = normalize_sku(bs)                # 検索キー
                g = sku_groups.setdefault(bk, {
                    'base_sku': bs,
                    'fba': {acc: 0 for acc in amazon_account_names},  # アカウント→販売可在庫
                    'fba_total_per_acc': {acc: 0 for acc in amazon_account_names},  # アカウント→総在庫
                    'rsl_qty': 0,        # 楽天合計（販売可）
                    'rsl_variants': 0,   # 楽天バリアント数
                    'yahoo_qty': 0,      # Yahoo合計
                    'yahoo_variants': 0, # Yahooバリアント数
                    'inbound': 0,        # FBA入庫予定（working+shipped+receiving）
                    'names': [],         # 商品名候補
                    'asins': set(),      # ASIN
                    'amazon_skus': [],   # 元のAmazonバリアントSKU
                    'amazon_sku_asin': {}, # SKU → ASIN マッピング（レート重複排除用）
                    'rakuten_skus': [],  # 元の楽天バリアントSKU
                    'yahoo_skus': [],    # 元のYahooバリアントSKU
                    'is_active': False,
                })
                if item.product_name and item.product_name not in g['names']:
                    g['names'].append(item.product_name)
                if item.asin:
                    g['asins'].add(item.asin)

                fq = max(item.fulfillable_quantity, 0)
                tq = max(item.total_quantity, 0)

                if item.marketplace == 'amazon':
                    acc = item.account_name
                    if acc in g['fba']:
                        g['fba'][acc] = g['fba'].get(acc, 0) + fq
                        g['fba_total_per_acc'][acc] = g['fba_total_per_acc'].get(acc, 0) + tq
                    g['inbound'] += (item.inbound_working_quantity + item.inbound_shipped_quantity + item.inbound_receiving_quantity)
                    g['amazon_skus'].append(item.seller_sku)
                    if item.asin:
                        g['amazon_sku_asin'][item.seller_sku] = item.asin
                    if tq > 0 or item.inbound_working_quantity > 0 or item.inbound_shipped_quantity > 0 or item.inbound_receiving_quantity > 0:
                        g['is_active'] = True
                elif item.marketplace == 'rakuten':
                    g['rsl_qty'] += fq
                    g['rsl_variants'] += 1
                    g['rakuten_skus'].append(item.seller_sku)
                    if fq > 0:
                        g['is_active'] = True
                elif item.marketplace == 'yahoo':
                    g['yahoo_qty'] += fq
                    g['yahoo_variants'] += 1
                    g['yahoo_skus'].append(item.seller_sku)
                    if fq > 0:
                        g['is_active'] = True

            # ── 各グループからrowを生成 ──
            rows = []
            for bk, g in sku_groups.items():
                fba_total = sum(g['fba'].values())
                rsl_qty = g['rsl_qty']
                yahoo_qty = g['yahoo_qty']
                # 全API在庫合計（販売可）
                api_total = fba_total + rsl_qty + yahoo_qty

                # SKUマスターからLT・発注中・発注推奨数（基本SKUで引く）
                master = sku_master_normalized.get(bk)
                lt_days    = master.lt_days   if master else 0
                in_transit = master.in_transit if master else 0
                order_qty  = master.order_qty  if master else 0
                order_date = master.order_date if master else ""

                # 入庫予定日 = 発注日 + リードタイム
                arrival_date = ""
                if order_date and lt_days > 0:
                    try:
                        from datetime import datetime as _dt, timedelta as _td
                        # order_dateは "2026/06/21" のような形式
                        d0 = None
                        for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y/%-m/%-d"):
                            try:
                                d0 = _dt.strptime(order_date.strip(), fmt)
                                break
                            except ValueError:
                                continue
                        if d0:
                            arrival_date = (d0 + _td(days=int(lt_days))).strftime("%Y/%m/%d")
                    except Exception:
                        arrival_date = ""

                arase_qty = arase_normalized.get(bk, 0)
                china_qty = china_normalized.get(bk, 0)
                office_qty = office_normalized.get(bk, 0)

                # ── プラットフォーム別 日次販売速度 ──
                # Amazon: 同ASIN内では同値が入っているのでASIN単位で重複排除
                # 楽天:   各バリアント (manageNumber:variantId) のレートを合算
                # Yahoo:  公開APIから販売データ取得不可（None）
                def _amazon_rate(rate_dict):
                    asin_rates: dict = {}
                    for sku, asin in g['amazon_sku_asin'].items():
                        if sku in rate_dict and asin:
                            asin_rates[asin] = rate_dict[sku]
                    s = sum(asin_rates.values())
                    return s if s > 0 else None

                def _rakuten_rate(rate_dict):
                    """楽天レート計算.

                    優先順位:
                    1) variant単位 (例 tg-01:tg-01l-red) がキャッシュにあればそれを使う
                    2) 無くてかつ「同manage番号(tg-01)の他variantキーが1つも無い」場合のみ
                       管理番号単位 (tg-01) の値を使う（バリアント情報が取れなかった商品）
                    3) 同manage番号の別variantキーが既にある場合、このvariantは0扱い
                       （= ゼロ販売）。商品単位フォールバックを使うと過大計上になる。
                    """
                    total = 0.0
                    for sku in g['rakuten_skus']:
                        if sku in rate_dict:
                            total += rate_dict[sku]
                            continue
                        # variant別が無い → 同manage番号の他キャッシュキー存在を確認
                        mn = sku.split(':')[0] if ':' in sku else sku
                        prefix = f'{mn}:'
                        has_other_variants = any(k.startswith(prefix) for k in rate_dict)
                        if not has_other_variants and mn in rate_dict:
                            # バリアント単位データが完全に存在しない場合のみ商品単位を使う
                            total += rate_dict[mn]
                    return total if total > 0 else None

                # プラットフォーム別レート (7日 / 30日 / 90日)
                amazon_rate_7d   = _amazon_rate(rates_7d)
                amazon_rate_30d  = _amazon_rate(rates_30d)
                amazon_rate_90d  = _amazon_rate(rates_90d)
                rakuten_rate_7d  = _rakuten_rate(rates_7d)
                rakuten_rate_30d = _rakuten_rate(rates_30d)
                rakuten_rate_90d = _rakuten_rate(rates_90d)
                # 旧キャッシュ互換（フォールバック用）
                amazon_rate_legacy  = _amazon_rate(cached_rates)
                rakuten_rate_legacy = _rakuten_rate(cached_rates)

                # ── プラットフォーム別に行を分割 ──
                # ステータス・アラート判定は _make_row 内で行毎に実施
                # 共通フィールド（全行で同じ値）
                common = {
                    'sku': g['base_sku'],
                    'name': (g['names'][0][:40] if g['names'] else ''),
                    'in_transit': in_transit,
                    'arase_qty': arase_qty,
                    'china_qty': china_qty,
                    'office_qty': office_qty,
                    'lt_days': lt_days,
                    'order_qty': order_qty,
                    'order_date': order_date,
                    'arrival_date': arrival_date,
                    'active': g['is_active'],
                }

                def _make_row(platform, own_qty, fba_by_account=None, rsl=0, yahoo=0, accounts=None):
                    # ── プラットフォーム別レート ──
                    if platform == 'amazon':
                        r7, r30, r90 = amazon_rate_7d, amazon_rate_30d, amazon_rate_90d
                        legacy_rate = amazon_rate_legacy
                    elif platform == 'rakuten':
                        r7, r30, r90 = rakuten_rate_7d, rakuten_rate_30d, rakuten_rate_90d
                        legacy_rate = rakuten_rate_legacy
                    else:  # yahoo: APIから販売データ取得不可
                        r7, r30, r90 = None, None, None
                        legacy_rate = None

                    # 残り日数計算用: 30日優先 → 7日 → 90日 → 旧キャッシュ
                    base_rate = r30 or r7 or r90 or legacy_rate

                    # この行の「全在庫」 = own + 共通(発注中・荒瀬・中国)
                    eff = own_qty + in_transit + arase_qty + china_qty + office_qty

                    dl_self = round(own_qty / base_rate) if (own_qty > 0 and base_rate) else None
                    dl_eff  = round(eff / base_rate)     if (eff > 0 and base_rate) else None
                    dl_7    = round(own_qty / r7)        if (own_qty > 0 and r7)        else None
                    dl_30   = round(own_qty / r30)       if (own_qty > 0 and r30)       else None
                    dl_90   = round(own_qty / r90)       if (own_qty > 0 and r90)       else None
                    # セール考慮版（calendarを使う）
                    if own_qty > 0 and base_rate:
                        dl_self = round(calendar.projected_days_remaining(own_qty, base_rate))
                    if eff > 0 and base_rate:
                        dl_eff = round(calendar.projected_days_remaining(eff, base_rate))

                    # ステータス
                    if own_qty == 0:
                        st = '在庫切れ'
                    elif own_qty <= defaults.critical_level:
                        st = '危険'
                    elif own_qty <= defaults.reorder_point:
                        st = '要発注'
                    else:
                        st = '正常'
                    lt_a = (lt_days > 0 and dl_eff is not None and dl_eff <= lt_days)
                    a_30 = dl_self is not None and dl_self < 30
                    return {
                        **common,
                        'platform': platform,
                        'accounts': accounts or [],
                        'fba_by_account': fba_by_account or {acc: 0 for acc in amazon_account_names},
                        'fba_total': sum((fba_by_account or {}).values()),
                        'rsl_qty': rsl,
                        'yahoo_qty': yahoo,
                        'qty': own_qty,
                        'effective_qty': eff,
                        'rate_7d':  round(r7, 2)  if r7  else None,
                        'rate_30d': round(r30, 2) if r30 else None,
                        'rate_90d': round(r90, 2) if r90 else None,
                        'days_left': dl_self,
                        'effective_days': dl_eff,
                        'days_left_7d':  dl_7,
                        'days_left_30d': dl_30,
                        'days_left_90d': dl_90,
                        'lt_alert': lt_a,
                        'alert_30': a_30,
                        'status': st,
                        'rsl_variants': g['rsl_variants'] if platform == 'rakuten' else 0,
                        'yahoo_variants': g['yahoo_variants'] if platform == 'yahoo' else 0,
                        'amazon_variant_count': len(g['amazon_skus']) if platform == 'amazon' else 0,
                    }

                # Amazon 行（FBA合計 > 0 または いずれかのアカウントにアクティブ在庫あり）
                amazon_active = fba_total > 0 or any(g['fba_total_per_acc'].get(a, 0) > 0 for a in amazon_account_names)
                if amazon_active:
                    accs_with_stock = [a for a in amazon_account_names if g['fba'].get(a, 0) > 0]
                    rows.append(_make_row('amazon', fba_total, fba_by_account=g['fba'], accounts=accs_with_stock))

                # 楽天 行
                if rsl_qty > 0:
                    rows.append(_make_row('rakuten', rsl_qty, rsl=rsl_qty))

                # Yahoo 行
                if yahoo_qty > 0:
                    rows.append(_make_row('yahoo', yahoo_qty, yahoo=yahoo_qty))

            total = len(rows)
            lt_alert_count = sum(1 for r in rows if r['lt_alert'])

            data = {
                'amazon_accounts': amazon_account_names,
                'rows': rows,
                'stats': {
                    'total': total,
                    'out': sum(1 for r in rows if r['status'] == '在庫切れ'),
                    'critical': sum(1 for r in rows if r['status'] == '危険'),
                    'warning': sum(1 for r in rows if r['status'] == '要発注'),
                    'ok': sum(1 for r in rows if r['status'] == '正常'),
                    'alert_30': sum(1 for r in rows if r['alert_30']),
                    'lt_alert': lt_alert_count,
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

        # キャッシュ保存（エラー時は保存しない）
        with _DATA_LOCK:
            try:
                d = json.loads(body)
                if d.get('rows'):
                    _DATA_CACHE['body'] = body
                    _DATA_CACHE['expires_at'] = _time.time() + _CACHE_TTL_SEC
            except Exception:
                pass

        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('X-Cache', 'MISS')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3737
    print(f'Preview server: http://localhost:{port}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
