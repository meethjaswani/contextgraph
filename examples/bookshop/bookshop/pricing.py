"""Price calculation: subtotal, discounts and tax."""

from .config import TAX_RATE


def subtotal(lines) -> float:
    return sum(book.price * qty for book, qty in lines)


def apply_discount(amount: float, code: str | None) -> float:
    """Codes: SAVE10 takes 10% off, FIVEOFF takes 5 off orders over 20."""
    if code == "SAVE10":
        return round(amount * 0.9, 2)
    if code == "FIVEOFF" and amount > 20:
        return amount - 5
    return amount


def with_tax(amount: float) -> float:
    return round(amount * (1 + TAX_RATE), 2)


def total(lines, code: str | None = None) -> float:
    """Subtotal, then discount, then tax."""
    return with_tax(apply_discount(subtotal(lines), code))
