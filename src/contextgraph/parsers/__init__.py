"""Parser registry. Add a language by subclassing `LanguageParser` and calling `register`."""

from __future__ import annotations

from contextgraph.parsers.base import LanguageParser
from contextgraph.parsers.python import PythonParser

_BY_EXTENSION: dict[str, LanguageParser] = {}


def register(parser: LanguageParser) -> None:
    for ext in parser.extensions:
        _BY_EXTENSION[ext] = parser


def parser_for(path: str) -> LanguageParser | None:
    dot = path.rfind(".")
    return _BY_EXTENSION.get(path[dot:]) if dot != -1 else None


def supported_extensions() -> tuple[str, ...]:
    return tuple(_BY_EXTENSION)


register(PythonParser())

__all__ = ["LanguageParser", "PythonParser", "parser_for", "register", "supported_extensions"]
