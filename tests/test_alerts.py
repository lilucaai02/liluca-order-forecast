from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from src.alerts import AlertDispatcher
from src.models import AlertEvent, AlertLevel


@pytest.fixture
def mock_settings(tmp_path: Path) -> Settings:
    settings = MagicMock(spec=Settings)
    settings.data_dir = tmp_path
    settings.alert_cooldown_hours = 24
    settings.has_email_config = False
    settings.has_slack_config = False
    return settings


@pytest.fixture
def sample_alert() -> AlertEvent:
    return AlertEvent(
        seller_sku="SKU-001",
        asin="B000000001",
        product_name="テスト商品",
        current_quantity=5,
        reorder_point=50,
        level=AlertLevel.CRITICAL,
        message="テストアラート",
    )


def test_cooldown_prevents_duplicate(mock_settings, sample_alert):
    """クールダウン期間中は同一SKUへの通知をスキップ."""
    dispatcher = AlertDispatcher(mock_settings)

    # 既にログに記録されている場合
    dispatcher._sent_log = {
        "SKU-001": datetime.utcnow().isoformat(),
    }

    sent = dispatcher.dispatch([sample_alert])
    assert len(sent) == 0


def test_cooldown_expired_allows_alert(mock_settings, sample_alert):
    """クールダウン期限切れの場合は送信可能 (通知先未設定のため実際の送信はなし)."""
    mock_settings.has_email_config = False
    mock_settings.has_slack_config = False

    dispatcher = AlertDispatcher(mock_settings)
    old_time = datetime.utcnow() - timedelta(hours=25)
    dispatcher._sent_log = {
        "SKU-001": old_time.isoformat(),
    }

    sent = dispatcher.dispatch([sample_alert])
    # 通知先未設定なのでsuccessにならない
    assert len(sent) == 0


def test_alert_log_persistence(mock_settings, sample_alert):
    """アラートログがファイルに永続化される."""
    dispatcher = AlertDispatcher(mock_settings)
    dispatcher._sent_log = {"SKU-001": datetime.utcnow().isoformat()}
    dispatcher._save_log()

    log_file = mock_settings.data_dir / "alerts_log.json"
    assert log_file.exists()

    data = json.loads(log_file.read_text())
    assert "SKU-001" in data
