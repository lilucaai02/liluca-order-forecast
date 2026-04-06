"""楽天RMS Inventory API 2.1 クライアント."""

from __future__ import annotations

import base64
import logging
from typing import Any

import requests

from config.settings import RakutenAccountConfig

logger = logging.getLogger(__name__)

# 楽天RMS API ベースURL
RMS_API_BASE = "https://api.rms.rakuten.co.jp/es/2.1"


class RakutenClient:
    """楽天RMS Inventory API 2.1 ラッパー."""

    def __init__(self, account: RakutenAccountConfig) -> None:
        self._account = account
        self._session = requests.Session()
        self._session.headers.update(self._build_headers())

    @property
    def account_name(self) -> str:
        return self._account.name

    def _build_headers(self) -> dict[str, str]:
        """ESA認証ヘッダーを構築."""
        credential = f"{self._account.service_secret}:{self._account.license_key}"
        encoded = base64.b64encode(credential.encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"ESA {encoded}",
        }

    def get_inventory_bulk(self) -> list[dict[str, Any]]:
        """inventories/bulk-get/range で全在庫を取得 (GET, クエリパラメータ)."""
        url = f"{RMS_API_BASE}/inventories/bulk-get/range"
        all_items: list[dict[str, Any]] = []

        params: dict[str, Any] = {
            "minQuantity": 0,
            "maxQuantity": 999999,
        }

        try:
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error("[楽天:%s] 在庫取得エラー: %s", self._account.name, e)
            raise

        # レスポンス: {"inventories": [{"manageNumber": "...", "variantId": "...", "quantity": N, ...}]}
        inventories = data.get("inventories", [])
        for inv in inventories:
            all_items.append({
                "manageNumber": inv.get("manageNumber", ""),
                "variantId": inv.get("variantId", ""),
                "quantity": inv.get("quantity", 0),
            })

        logger.info("[楽天:%s] %d件の在庫を取得", self._account.name, len(all_items))
        return all_items
