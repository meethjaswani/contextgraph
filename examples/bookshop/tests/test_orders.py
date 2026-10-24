from bookshop.cli import build_demo_catalog
from bookshop.orders import quote


def test_quote():
    assert quote(build_demo_catalog(), {"222": 2}) == 9.72
