"""Placing orders: reserve stock, price, charge, notify."""

from dataclasses import dataclass, field

from . import pricing
from .catalog import Catalog
from .notify import send_receipt
from .payments import get_gateway


@dataclass
class Order:
    id: str
    lines: list = field(default_factory=list)
    amount: float = 0.0
    charge_id: str = ""


def place_order(catalog: Catalog, items: dict, email: str, token: str, code=None, gateway="card") -> Order:
    """Reserve every item, charge the card and email a receipt."""
    lines = [(catalog.reserve(isbn, qty), qty) for isbn, qty in items.items()]
    order = Order(id=f"o-{len(lines)}-{email}", lines=lines)
    order.amount = pricing.total(lines, code)
    order.charge_id = get_gateway(gateway).charge(order.amount, token)
    send_receipt(email, order)
    return order


def quote(catalog: Catalog, items: dict, code=None) -> float:
    """Price an order without reserving stock."""
    lines = [(catalog.find(isbn), qty) for isbn, qty in items.items()]
    return pricing.total(lines, code)
