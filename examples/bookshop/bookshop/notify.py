"""Order emails."""

import smtplib

from .config import load_settings


def send_receipt(email: str, order) -> None:
    settings = load_settings()
    body = f"Thanks! Order {order.id} total {order.amount} {settings['currency']}"
    with smtplib.SMTP(settings["smtp_host"]) as smtp:
        smtp.sendmail("shop@example.com", [email], body)
