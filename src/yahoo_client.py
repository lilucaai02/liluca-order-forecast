"""Yahoo!ショッピング ストアAPI クライアント.

認証方式:
  - 在庫読み取り: appid (クライアントID) + seller_id でアクセス (V3 itemSearch)
    ※ 公開APIのため在庫個数は取得不可。inStock (true/false) のみ。
    ※ 在庫個数取得には OAuth 2.0 + ストア管理API が必要 (yahoo_auth.py 参照)
  - 将来の在庫更新: OAuth 2.0 (yahoo_auth.py でトークン取得)

公式ドキュメント:
  https://developer.yahoo.co.jp/webapi/shopping/
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Yahoo!ショッピング 商品検索API (V3) - seller_id でストア絞り込み可
ITEM_SEARCH_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"

# OAuth 2.0 トークンエンドポイント
YAHOO_TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"

# ストア管理API (OAuth Bearer 認証)
STORE_API_BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"
ORDER_LIST_URL = f"{STORE_API_BASE}/orderList"     # 注文検索 (POST XML)
ORDER_INFO_URL = f"{STORE_API_BASE}/orderInfo"     # 注文詳細 (POST XML)
GET_STOCK_URL  = f"{STORE_API_BASE}/getStock"      # 在庫取得 (POST form)

# 1ページあたりの取得件数 (V3公開API・最大100)
PAGE_SIZE = 100

# ストア管理 orderList の1リクエスト最大件数 (仕様上2000だが安全のため)
ORDER_LIST_PAGE = 500

# getStock 1リクエストの item_code 上限 (仕様: 1000)
STOCK_BATCH = 500

# Yahoo API のレート制限 (約1QPS)
YAHOO_QPS_SLEEP = 1.1


class YahooClient:
    """Yahoo!ショッピング ストアAPI ラッパー."""

    def __init__(
        self,
        account_name: str,
        client_id: str,
        seller_id: str,
        client_secret: str = "",
        access_token: str = "",
        refresh_token: str = "",
    ) -> None:
        self._account_name = account_name
        self._client_id = client_id
        self._seller_id = seller_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._session = requests.Session()

    @property
    def account_name(self) -> str:
        return self._account_name

    @property
    def seller_id(self) -> str:
        return self._seller_id

    def _get_headers(self) -> dict[str, str]:
        """認証ヘッダー (OAuth トークンがあれば使用)."""
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        return {}

    def refresh_access_token(self) -> str:
        """リフレッシュトークンでアクセストークンを更新."""
        if not self._refresh_token or not self._client_secret:
            raise ValueError("refresh_token と client_secret が必要です")

        import base64
        credentials = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()

        resp = requests.post(
            YAHOO_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        logger.info("[Yahoo:%s] アクセストークン更新完了", self._account_name)
        return self._access_token

    def get_store_items(self) -> list[dict[str, Any]]:
        """ストア全商品を取得 (ページネーション対応).

        Yahoo! Shopping V3 itemSearch API を使用。
        seller_id でストア絞り込み。inStock フラグのみ取得可能。
        """
        all_items: list[dict[str, Any]] = []
        start = 1

        while True:
            params: dict[str, Any] = {
                "appid": self._client_id,
                "seller_id": self._seller_id,
                "results": PAGE_SIZE,
                "start": start,
            }

            try:
                resp = self._session.get(
                    ITEM_SEARCH_URL,
                    params=params,
                    headers=self._get_headers(),
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                logger.error("[Yahoo:%s] 商品取得エラー: %s", self._account_name, e)
                raise

            hits = data.get("hits", [])
            if not hits:
                break

            all_items.extend(hits)

            total_available = int(data.get("totalResultsAvailable", 0) or len(all_items))

            logger.debug(
                "[Yahoo:%s] 取得中: %d / %d",
                self._account_name, len(all_items), total_available
            )

            if len(all_items) >= total_available or len(hits) < PAGE_SIZE:
                break

            start += PAGE_SIZE
            time.sleep(0.3)  # レートリミット対策

        logger.info("[Yahoo:%s] %d件の商品を取得", self._account_name, len(all_items))
        return all_items

    def extract_stock_qty(self, item: dict[str, Any]) -> int:
        """商品データから在庫数を抽出.

        公開APIでは実在庫数は取得不可。inStock=True なら 1、False なら 0 を返す。
        """
        in_stock = item.get("inStock", False)
        return 1 if in_stock else 0

    def extract_sku(self, item: dict[str, Any]) -> str:
        """商品コード (SKU相当) を抽出.

        V3 API の code フィールドは "{seller_id}_{item_code}" 形式。
        seller_id プレフィックスを除去して item_code のみ返す。
        """
        code = str(item.get("code") or item.get("Code") or "").strip()
        prefix = f"{self._seller_id}_"
        if code.startswith(prefix):
            code = code[len(prefix):]
        return code

    @staticmethod
    def extract_name(item: dict[str, Any]) -> str:
        """商品名を抽出."""
        return str(item.get("name") or item.get("Name") or item.get("headLine") or "")

    # ---------------------------------------------------------------
    # ストア管理API (OAuth必須)
    # ---------------------------------------------------------------

    def _ensure_access_token(self) -> str:
        """アクセストークンを保証（未取得ならrefresh_tokenで取得）."""
        if not self._access_token:
            self.refresh_access_token()
        return self._access_token

    def _store_headers_xml(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_access_token()}",
            "Content-Type": "application/xml; charset=utf-8",
        }

    def _store_headers_form(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_access_token()}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def get_store_stock(self, item_codes: list[str]) -> dict[str, int]:
        """ストア管理API getStock で在庫数を取得.

        Args:
            item_codes: 商品コードのリスト（"code" または "code:subcode"）

        Returns:
            {item_code: quantity} の辞書。空欄(無制限)は -1、取得失敗は0。
        """
        import xml.etree.ElementTree as ET

        result: dict[str, int] = {}
        for i in range(0, len(item_codes), STOCK_BATCH):
            batch = item_codes[i:i + STOCK_BATCH]
            body = f"seller_id={self._seller_id}&item_code=" + ",".join(batch)

            try:
                resp = self._session.post(
                    GET_STOCK_URL,
                    headers=self._store_headers_form(),
                    data=body.encode("utf-8"),
                    timeout=60,
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.error("[Yahoo:%s] getStock失敗: %s", self._account_name, e)
                raise

            # レスポンスXMLをパース
            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError as e:
                logger.error("[Yahoo:%s] getStock XML parse失敗: %s", self._account_name, e)
                continue

            # ネームスペースを剥がす（{http://…}Result → Result）
            for elem in root.iter():
                if "}" in elem.tag:
                    elem.tag = elem.tag.split("}", 1)[1]

            # 各 <Result> ノード内に <ItemCode><SubCode><Quantity> がある想定
            for r in root.iter("Result"):
                code = (r.findtext("ItemCode") or "").strip()
                sub = (r.findtext("SubCode") or "").strip()
                qty_str = (r.findtext("Quantity") or "").strip()
                if not code:
                    continue
                key = f"{code}:{sub}" if sub else code
                try:
                    qty = int(qty_str) if qty_str else -1  # 空欄=無制限
                except ValueError:
                    qty = 0
                result[key] = qty

            time.sleep(YAHOO_QPS_SLEEP)

        logger.info("[Yahoo:%s] getStock: %d件取得", self._account_name, len(result))
        return result

    def search_orders(
        self,
        from_dt: str,
        to_dt: str,
        order_status: list[int] | None = None,
    ) -> list[str]:
        """orderList で期間内の注文番号を取得（ページング対応）.

        Args:
            from_dt: 開始日時 YYYYMMDDHH24MISS 形式
            to_dt:   終了日時 YYYYMMDDHH24MISS 形式
            order_status: フィルタする注文ステータス（None なら全ステータス）

        Returns:
            注文番号(OrderId)のリスト
        """
        import xml.etree.ElementTree as ET

        order_ids: list[str] = []
        start = 1

        status_xml = ""
        if order_status:
            for s in order_status:
                status_xml += f"<OrderStatus>{s}</OrderStatus>"

        while True:
            body_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Req>
  <Search>
    <Result>{ORDER_LIST_PAGE}</Result>
    <Start>{start}</Start>
    <Sort>+order_time</Sort>
    <Condition>
      <OrderTimeFrom>{from_dt}</OrderTimeFrom>
      <OrderTimeTo>{to_dt}</OrderTimeTo>
      {status_xml}
    </Condition>
    <Field>OrderId</Field>
  </Search>
  <SellerId>{self._seller_id}</SellerId>
</Req>"""
            try:
                resp = self._session.post(
                    ORDER_LIST_URL,
                    headers=self._store_headers_xml(),
                    data=body_xml.encode("utf-8"),
                    timeout=60,
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.error("[Yahoo:%s] orderList失敗: %s", self._account_name, e)
                raise

            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError as e:
                logger.error("[Yahoo:%s] orderList XML parse失敗: %s", self._account_name, e)
                break
            for elem in root.iter():
                if "}" in elem.tag:
                    elem.tag = elem.tag.split("}", 1)[1]

            batch_ids: list[str] = []
            for oi in root.iter("OrderInfo"):
                oid = (oi.findtext("OrderId") or "").strip()
                if oid:
                    batch_ids.append(oid)
            order_ids.extend(batch_ids)

            total_str = root.findtext(".//Search/TotalCount") or "0"
            try:
                total = int(total_str)
            except ValueError:
                total = 0
            logger.info(
                "[Yahoo:%s] orderList start=%d 取得=%d 累計=%d/%d",
                self._account_name, start, len(batch_ids), len(order_ids), total,
            )

            if len(batch_ids) < ORDER_LIST_PAGE or len(order_ids) >= total:
                break
            start += ORDER_LIST_PAGE
            time.sleep(YAHOO_QPS_SLEEP)

        return order_ids

    def get_order_detail(self, order_id: str) -> dict[str, Any]:
        """orderInfo で1注文の詳細（商品明細含む）を取得.

        Returns:
            {
              "order_id": str,
              "order_time": str (ISO8601),
              "items": [{"item_id": str, "sub_code": str, "quantity": int, "unit_price": float}]
            }
        """
        import xml.etree.ElementTree as ET

        body_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Req>
  <Target>
    <OrderId>{order_id}</OrderId>
    <Field>OrderId,OrderTime,OrderStatus,ItemId,SubCode,Quantity,UnitPrice</Field>
  </Target>
  <SellerId>{self._seller_id}</SellerId>
</Req>"""
        try:
            resp = self._session.post(
                ORDER_INFO_URL,
                headers=self._store_headers_xml(),
                data=body_xml.encode("utf-8"),
                timeout=60,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("[Yahoo:%s] orderInfo失敗 order_id=%s: %s",
                         self._account_name, order_id, e)
            raise

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            logger.error("[Yahoo:%s] orderInfo XML parse失敗: %s", self._account_name, e)
            return {"order_id": order_id, "order_time": "", "items": []}
        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        order_time = ""
        for oi in root.iter("OrderInfo"):
            order_time = (oi.findtext("OrderTime") or "").strip()
            break

        items: list[dict[str, Any]] = []
        for it in root.iter("Item"):
            item_id = (it.findtext("ItemId") or "").strip()
            sub_code = (it.findtext("SubCode") or "").strip()
            qty_str = (it.findtext("Quantity") or "0").strip()
            price_str = (it.findtext("UnitPrice") or "0").strip()
            try:
                qty = int(qty_str)
            except ValueError:
                qty = 0
            try:
                price = float(price_str)
            except ValueError:
                price = 0.0
            if item_id:
                items.append({
                    "item_id": item_id,
                    "sub_code": sub_code,
                    "quantity": qty,
                    "unit_price": price,
                })

        return {"order_id": order_id, "order_time": order_time, "items": items}
