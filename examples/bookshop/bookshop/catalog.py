"""Books and the in-memory catalog."""

from dataclasses import dataclass


@dataclass
class Book:
    isbn: str
    title: str
    price: float
    stock: int = 0


class Catalog:
    """Holds books by ISBN."""

    def __init__(self):
        self._books: dict[str, Book] = {}

    def add(self, book: Book) -> None:
        self._books[book.isbn] = book

    def find(self, isbn: str) -> Book:
        """Look up a book, raising KeyError if it is unknown."""
        return self._books[isbn]

    def reserve(self, isbn: str, qty: int) -> Book:
        """Take `qty` copies out of stock."""
        book = self.find(isbn)
        if book.stock < qty:
            raise ValueError(f"only {book.stock} left of {book.title}")
        book.stock -= qty
        return book
