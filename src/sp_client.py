from __future__ import annotations

from typing import TYPE_CHECKING

from sp_api.api import Inventories, Sales
from sp_api.base import Marketplaces, SellingApiException

if TYPE_CHECKING:
    from config.settings import Settings

MARKETPLACE_MAP = {
    "JP": Marketplaces.JP,
    "US": Marketplaces.US,
    "UK": Marketplaces.UK,
    "DE": Marketplaces.DE,
}

MARKETPLACE_ID_MAP = {
    "JP": "A1VC38T7YXB528",
    "US": "ATVPDKIKX0DER",
    "UK": "A1F83G8C2ARO7P",
    "DE": "A1PA6795UKMFR9",
}


class SPClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._marketplace = MARKETPLACE_MAP[settings.marketplace.value]
        self._marketplace_id = MARKETPLACE_ID_MAP[settings.marketplace.value]
        self._credentials: dict = {
            "refresh_token": settings.sp_api_refresh_token,
            "lwa_app_id": settings.lwa_app_id,
            "lwa_client_secret": settings.lwa_client_secret,
        }
        if settings.sp_api_access_key:
            self._credentials["aws_access_key"] = settings.sp_api_access_key
            self._credentials["aws_secret_key"] = settings.sp_api_secret_key
            self._credentials["role_arn"] = settings.sp_api_role_arn

    def _make_client(self, api_class: type):
        return api_class(
            marketplace=self._marketplace,
            credentials=self._credentials,
        )

    @property
    def marketplace_id(self) -> str:
        return self._marketplace_id

    def get_inventory_summaries(
        self,
        seller_skus: list[str] | None = None,
    ) -> list[dict]:
        """FBA在庫サマリーを全件取得 (ページネーション対応)."""
        client = self._make_client(Inventories)
        all_summaries: list[dict] = []
        next_token: str | None = None

        while True:
            kwargs: dict = {
                "granularityType": "Marketplace",
                "granularityId": self._marketplace_id,
                "marketplaceIds": [self._marketplace_id],
                "details": True,
            }
            if seller_skus:
                kwargs["sellerSkus"] = seller_skus
            if next_token:
                kwargs["nextToken"] = next_token

            try:
                response = client.get_inventory_summary_marketplace(**kwargs)
            except SellingApiException as e:
                raise RuntimeError(f"SP-API inventory error: {e}") from e

            payload = response.payload or {}
            summaries = payload.get("inventorySummaries", [])
            all_summaries.extend(summaries)

            pagination = payload.get("pagination", {})
            next_token = pagination.get("nextToken")
            if not next_token:
                break

        return all_summaries

    def get_order_metrics(
        self,
        interval_start: str,
        interval_end: str,
        granularity: str = "Day",
    ) -> list[dict]:
        """販売メトリクスを取得."""
        client = self._make_client(Sales)
        try:
            response = client.get_order_metrics(
                interval=(interval_start, interval_end),
                granularity=granularity,
                granularityTimeZone="Asia/Tokyo",
            )
        except SellingApiException as e:
            raise RuntimeError(f"SP-API sales error: {e}") from e

        return response.payload or []
