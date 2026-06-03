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

# OAuth 2.0 トークンエンドポイント (将来の書き込み操作用)
YAHOO_TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"

# 1ページあたりの取得件数 (最大100)
PAGE_SIZE = 100


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
