import argparse

from bookshop import Catalog, place_order
from bookshop.catalog import Book
from bookshop.orders import quote


def build_demo_catalog() -> Catalog:
    catalog = Catalog()
    catalog.add(Book("111", "Dune", 9.99, stock=3))
    catalog.add(Book("222", "Emma", 4.50, stock=10))
    return catalog


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--quote", action="store_true")
    args = p.parse_args(argv)
    catalog = build_demo_catalog()
    if args.quote:
        print(quote(catalog, {"111": 1}))
    else:
        print(place_order(catalog, {"111": 1}, "a@b.c", "tok_x", gateway="fake"))


if __name__ == "__main__":
    main()
