from __future__ import annotations

import json
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from src.models import AlertEvent, AlertLevel

if TYPE_CHECKING:
    from config.settings import Settings


class AlertDispatcher:
    """Email/Slackでアラート送信. クールダウンによる重複通知防止付き."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log_file = settings.data_dir / "alerts_log.json"
        self._cooldown = timedelta(hours=settings.alert_cooldown_hours)
        self._sent_log = self._load_log()

    def _load_log(self) -> dict[str, str]:
        if self._log_file.exists():
            return json.loads(self._log_file.read_text(encoding="utf-8"))
        return {}

    def _save_log(self) -> None:
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_file.write_text(
            json.dumps(self._sent_log, indent=2), encoding="utf-8"
        )

    def _is_in_cooldown(self, sku: str) -> bool:
        last_sent = self._sent_log.get(sku)
        if not last_sent:
            return False
        last_dt = datetime.fromisoformat(last_sent)
        return datetime.utcnow() - last_dt < self._cooldown

    def dispatch(self, alerts: list[AlertEvent]) -> list[AlertEvent]:
        """クールダウン外のアラートを送信. 実際に送信されたアラートを返す."""
        sent: list[AlertEvent] = []

        for alert in alerts:
            if self._is_in_cooldown(alert.seller_sku):
                continue

            success = False
            if self._settings.has_email_config:
                success |= self._send_email(alert)
            if self._settings.has_slack_config:
                success |= self._send_slack(alert)

            if success:
                self._sent_log[alert.seller_sku] = datetime.utcnow().isoformat()
                sent.append(alert)

        self._save_log()
        return sent

    def _send_email(self, alert: AlertEvent) -> bool:
        s = self._settings
        prefix = {
            AlertLevel.OUT_OF_STOCK: "[在庫切れ]",
            AlertLevel.CRITICAL: "[危険]",
            AlertLevel.WARNING: "[低在庫]",
        }
        subject = f"{prefix[alert.level]} {alert.seller_sku} - 在庫アラート"
        body = self._format_email_body(alert)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = s.smtp_user
        msg["To"] = s.alert_email_to
        msg.attach(MIMEText(body, "html", "utf-8"))

        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port) as server:
                server.starttls()
                server.login(s.smtp_user, s.smtp_password)
                server.sendmail(s.smtp_user, [s.alert_email_to], msg.as_string())
            return True
        except Exception as e:
            print(f"Email送信失敗: {e}")
            return False

    def _send_slack(self, alert: AlertEvent) -> bool:
        color_map = {
            AlertLevel.OUT_OF_STOCK: "#FF0000",
            AlertLevel.CRITICAL: "#FF6600",
            AlertLevel.WARNING: "#FFCC00",
        }
        payload = {
            "attachments": [
                {
                    "color": color_map[alert.level],
                    "title": f"在庫アラート: {alert.seller_sku}",
                    "text": alert.message,
                    "fields": [
                        {"title": "SKU", "value": alert.seller_sku, "short": True},
                        {"title": "現在庫数", "value": str(alert.current_quantity), "short": True},
                        {"title": "発注点", "value": str(alert.reorder_point), "short": True},
                    ],
                }
            ]
        }
        if alert.estimated_days_of_stock is not None:
            payload["attachments"][0]["fields"].append(
                {"title": "残日数", "value": str(alert.estimated_days_of_stock), "short": True}
            )
        if alert.suggested_reorder_qty:
            payload["attachments"][0]["fields"].append(
                {"title": "推奨発注数", "value": str(alert.suggested_reorder_qty), "short": True}
            )

        try:
            resp = requests.post(
                self._settings.slack_webhook_url, json=payload, timeout=10
            )
            return resp.status_code == 200
        except Exception as e:
            print(f"Slack送信失敗: {e}")
            return False

    @staticmethod
    def _format_email_body(alert: AlertEvent) -> str:
        rows = [
            ("SKU", alert.seller_sku),
            ("ASIN", alert.asin),
            ("商品名", alert.product_name),
            ("現在庫数", str(alert.current_quantity)),
            ("発注点", str(alert.reorder_point)),
            ("レベル", alert.level.value.upper()),
        ]
        if alert.avg_daily_sales is not None:
            rows.append(("平均日販数", f"{alert.avg_daily_sales:.1f}"))
        if alert.estimated_days_of_stock is not None:
            rows.append(("残日数", f"{alert.estimated_days_of_stock:.1f}"))
        if alert.suggested_reorder_qty:
            rows.append(("推奨発注数", str(alert.suggested_reorder_qty)))

        table_rows = "".join(
            f"<tr><td style='padding:4px 8px;font-weight:bold'>{k}</td>"
            f"<td style='padding:4px 8px'>{v}</td></tr>"
            for k, v in rows
        )
        return f"""
        <html><body>
        <h2 style="color:#d32f2f">在庫アラート</h2>
        <table border="1" cellspacing="0" style="border-collapse:collapse">
        {table_rows}
        </table>
        <p style="color:#666;font-size:12px">
            Amazon Inventory Monitor - {alert.timestamp.strftime("%Y-%m-%d %H:%M UTC")}
        </p>
        </body></html>
        """
