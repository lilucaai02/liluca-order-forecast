from __future__ import annotations

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # SP-API Credentials
    sp_api_refresh_token: str = ""
    lwa_app_id: str = ""
    lwa_client_secret: str = ""

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

    # Monitoring
    check_interval_minutes: int = 60
    alert_cooldown_hours: int = 24

    # Paths
    data_dir: Path = Field(default=Path("data"))
    thresholds_file: Path = Field(default=Path("config/thresholds.yaml"))

    def validate_credentials(self) -> list[str]:
        """Return list of missing required credentials."""
        missing = []
        if not self.sp_api_refresh_token:
            missing.append("SP_API_REFRESH_TOKEN")
        if not self.lwa_app_id:
            missing.append("LWA_APP_ID")
        if not self.lwa_client_secret:
            missing.append("LWA_CLIENT_SECRET")
        return missing

    @property
    def has_email_config(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and self.alert_email_to)

    @property
    def has_slack_config(self) -> bool:
        return bool(self.slack_webhook_url)
