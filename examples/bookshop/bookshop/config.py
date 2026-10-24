"""Settings read from the environment."""

import os

DEFAULT_CURRENCY = "USD"
TAX_RATE = float(os.environ.get("BOOKSHOP_TAX_RATE", "0.08"))


def load_settings() -> dict:
    """Return the shop settings as a dict."""
    return {
        "currency": os.environ.get("BOOKSHOP_CURRENCY", DEFAULT_CURRENCY),
        "tax_rate": TAX_RATE,
        "smtp_host": os.environ.get("BOOKSHOP_SMTP_HOST", "localhost"),
    }
