"""Parser interface. Each language implements `LanguageParser` and registers itself."""

from __future__ import annotations

from abc import ABC, abstractmethod

from contextgraph.models import FileResult


class LanguageParser(ABC):
    language: str
    extensions: tuple[str, ...]

    @abstractmethod
    def module_name(self, path: str) -> str:
        """Map a repo-relative path to the dotted module name used for resolution."""

    @abstractmethod
    def parse(self, path: str, source: bytes) -> FileResult:
        """Extract symbols, imports and calls from one file."""
