from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MarketplaceCode(str, Enum):
    JP = "JP"
    US = "US"
    UK = "UK"
    DE = "DE"


@dataclass
class AccountConfig:
    """1つのAmazonセラーアカウントの認証情報."""
    name: str
    refresh_token: str


@dataclass
class RakutenAccountConfig:
    """1つの楽天RMSアカウントの認証情報."""
    name: str
    service_secret: str
    license_key: str


@dataclass
class YahooAccountConfig:
    """1つのYahoo!ショッピングアカウントの認証情報."""
    name: str
    client_id: str        # Yahoo!デベロッパーネットワークのクライアントID (appid)
    seller_id: str        # Yahoo!ショッピングのストアID (出品者ID)
    client_secret: str = ""  # OAuthトークン更新用 (在庫書き込み時に必要)
    refresh_token: str = ""  # OAuthリフレッシュトークン


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # SP-API Credentials (共通)
    lwa_app_id: str = ""
    lwa_client_secret: str = ""

    # レガシー互換: 単一アカウント用
    sp_api_refresh_token: str = ""

    # マルチアカウント (ACCOUNT_1_NAME, ACCOUNT_1_REFRESH_TOKEN, ...)
    account_1_name: str = ""
    account_1_refresh_token: str = ""
    account_2_name: str = ""
    account_2_refresh_token: str = ""
    account_3_name: str = ""
    account_3_refresh_token: str = ""
    account_4_name: str = ""
    account_4_refresh_token: str = ""
    account_5_name: str = ""
    account_5_refresh_token: str = ""

    # 楽天RMS認証情報 (最大3アカウント)
    rakuten_1_name: str = ""
    rakuten_1_service_secret: str = ""
    rakuten_1_license_key: str = ""
    rakuten_2_name: str = ""
    rakuten_2_service_secret: str = ""
    rakuten_2_license_key: str = ""
    rakuten_3_name: str = ""
    rakuten_3_service_secret: str = ""
    rakuten_3_license_key: str = ""

    # Yahoo!ショッピング認証情報 (最大3アカウント)
    yahoo_1_name: str = ""
    yahoo_1_client_id: str = ""
    yahoo_1_seller_id: str = ""
    yahoo_1_client_secret: str = ""
    yahoo_1_refresh_token: str = ""
    yahoo_2_name: str = ""
    yahoo_2_client_id: str = ""
    yahoo_2_seller_id: str = ""
    yahoo_2_client_secret: str = ""
    yahoo_2_refresh_token: str = ""
    yahoo_3_name: str = ""
    yahoo_3_client_id: str = ""
    yahoo_3_seller_id: str = ""
    yahoo_3_client_secret: str = ""
    yahoo_3_refresh_token: str = ""

    # AWS (optional)
    sp_api_access_key: str = ""
    sp_api_secret_key: str = ""
    sp_api_role_arn: str = ""

    # Marketplace
    marketplace: MarketplaceCode = MarketplaceCode.JP

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_to: str = ""

    # Slack
    slack_webhook_url: str = ""

    # ChatWork
    chatwork_api_token: str = ""
    chatwork_room_id: str = ""

    # ngrok 公開アクセス用 Basic 認証
    ngrok_basic_user: str = ""
    ngrok_basic_pass: str = ""

    # Sandbox mode
    sandbox_mode: bool = True

    # Monitoring
    check_interval_minutes: int = 60
    alert_cooldown_hours: int = 24

    # Google Sheets (optional)
    google_credentials_file: str = ""
    google_spreadsheet_id: str = ""

    # Paths
    data_dir: Path = Field(default=Path("data"))
    thresholds_file: Path = Field(default=Path("config/thresholds.yaml"))

    def get_accounts(self) -> list[AccountConfig]:
        """設定済みのアカウント一覧を返す."""
        accounts: list[AccountConfig] = []
        for i in range(1, 6):
            name = getattr(self, f"account_{i}_name", "")
            token = getattr(self, f"account_{i}_refresh_token", "")
            if name and token:
                accounts.append(AccountConfig(name=name, refresh_token=token))

        # マルチアカウント未設定の場合、レガシーのsp_api_refresh_tokenを使用
        if not accounts and self.sp_api_refresh_token:
            accounts.append(
                AccountConfig(name="default", refresh_token=self.sp_api_refresh_token)
            )

        return accounts

    def get_account(self, name: str) -> AccountConfig | None:
        """名前でAmazonアカウントを検索."""
        for acc in self.get_accounts():
            if acc.name == name:
                return acc
        return None

    def get_rakuten_accounts(self) -> list[RakutenAccountConfig]:
        """設定済みの楽天アカウント一覧を返す."""
        accounts: list[RakutenAccountConfig] = []
        for i in range(1, 4):
            name = getattr(self, f"rakuten_{i}_name", "")
            secret = getattr(self, f"rakuten_{i}_service_secret", "")
            key = getattr(self, f"rakuten_{i}_license_key", "")
            if name and secret and key:
                accounts.append(
                    RakutenAccountConfig(name=name, service_secret=secret, license_key=key)
                )
        return accounts

    def get_rakuten_account(self, name: str) -> RakutenAccountConfig | None:
        """名前で楽天アカウントを検索."""
        for acc in self.get_rakuten_accounts():
            if acc.name == name:
                return acc
        return None

    def get_yahoo_accounts(self) -> list[YahooAccountConfig]:
        """設定済みのYahoo!ショッピングアカウント一覧を返す."""
        accounts: list[YahooAccountConfig] = []
        for i in range(1, 4):
            name = getattr(self, f"yahoo_{i}_name", "")
            client_id = getattr(self, f"yahoo_{i}_client_id", "")
            seller_id = getattr(self, f"yahoo_{i}_seller_id", "")
            client_secret = getattr(self, f"yahoo_{i}_client_secret", "")
            refresh_token = getattr(self, f"yahoo_{i}_refresh_token", "")
            if name and client_id and seller_id:
                accounts.append(YahooAccountConfig(
                    name=name,
                    client_id=client_id,
                    seller_id=seller_id,
                    client_secret=client_secret,
                    refresh_token=refresh_token,
                ))
        return accounts

    def validate_credentials(self) -> list[str]:
        """Return list of missing required credentials."""
        missing = []
        amazon_accounts = self.get_accounts()
        rakuten_accounts = self.get_rakuten_accounts()
        if not amazon_accounts and not rakuten_accounts:
            missing.append("ACCOUNT_*_REFRESH_TOKEN または RAKUTEN_*_SERVICE_SECRET")
        if amazon_accounts and not self.lwa_app_id:
            missing.append("LWA_APP_ID")
        if amazon_accounts and not self.lwa_client_secret:
            missing.append("LWA_CLIENT_SECRET")
        return missing

    @property
    def has_email_config(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and self.alert_email_to)

    @property
    def has_slack_config(self) -> bool:
        return bool(self.slack_webhook_url)

    @property
    def has_gsheet_config(self) -> bool:
        return bool(self.google_credentials_file and self.google_spreadsheet_id)
