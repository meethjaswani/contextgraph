"""MCP server exposing the graph queries as tools."""

from __future__ import annotations

import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from contextgraph import tools
from contextgraph.graph import CodeGraph
from contextgraph.indexer import discover, index_repo

INSTRUCTIONS = """ContextGraph indexes this repository as a graph of files, classes and functions
linked by imports, calls and inheritance. Prefer these tools over reading whole files:
start with repo_overview or search_symbol, then get_context for the code you need.
Symbols are qualified names like pkg.module.Class.method; short names work when unique."""


class _State:
    """Holds the graph and refreshes it when files on disk change (checked at most every few seconds)."""

    def __init__(self, root: Path, out: Path | None, check_every: float = 3.0) -> None:
        self.root = root
        self.out = out
        self.check_every = check_every
        self._cg: CodeGraph | None = None
        self._snapshot: dict[str, float] = {}
        self._checked = 0.0

    def _mtimes(self) -> dict[str, float]:
        snap = {}
        for rel in discover(self.root):
            try:
                snap[rel] = os.stat(self.root / rel).st_mtime
            except OSError:
                pass
        return snap

    def reindex(self) -> str:
        self._cg, stats = index_repo(self.root, self.out)
        self._snapshot = self._mtimes()
        self._checked = time.monotonic()
        return (f"indexed {stats.files} files ({stats.parsed} parsed, {stats.reused} unchanged, "
                f"{stats.removed} removed) in {stats.seconds}s")

    def graph(self) -> CodeGraph:
        if self._cg is None:
            self.reindex()
        elif time.monotonic() - self._checked > self.check_every:
            self._checked = time.monotonic()
            if self._mtimes() != self._snapshot:
                self.reindex()
        assert self._cg is not None
        return self._cg


def create_server(root: str | Path, out: str | Path | None = None) -> MCPServer:
    state = _State(Path(root).resolve(), Path(out) if out else None)
    server = MCPServer("contextgraph", instructions=INSTRUCTIONS)

    def run(fn, *args):
        try:
            return fn(state.graph(), *args)
        except tools.ToolError as e:
            return f"error: {e}"

    @server.tool()
    def search_symbol(query: str, kind: str | None = None, limit: int = 20) -> str:
        """Find classes, functions and methods by name (substring match, best matches first).
        kind: optional filter, one of "class", "function", "method"."""
        return run(tools.search_symbol, query, kind, limit)

    @server.tool()
    def get_definition(symbol: str) -> str:
        """Return the source code of one symbol, with its file and line range."""
        return run(tools.get_definition, symbol)

    @server.tool()
    def find_callers(symbol: str, depth: int = 1) -> str:
        """List the functions/methods that call `symbol`, with call-site lines.
        depth > 1 follows callers of callers (useful for "what breaks if I change this")."""
        return run(tools.find_callers, symbol, depth)

    @server.tool()
    def find_callees(symbol: str, depth: int = 1) -> str:
        """List what `symbol` calls: functions in the repo and external calls."""
        return run(tools.find_callees, symbol, depth)

    @server.tool()
    def get_dependencies(file: str) -> str:
        """List the modules a file imports, split into in-repo files and external packages."""
        return run(tools.get_dependencies, file)

    @server.tool()
    def get_dependents(file: str, depth: int = 1) -> str:
        """List the files that import `file`. depth > 1 includes indirect importers."""
        return run(tools.get_dependents, file, depth)

    @server.tool()
    def get_context(symbol: str, depth: int = 1, max_tokens: int = 4000) -> str:
        """The symbol's source plus its callers and callees up to `depth` hops, trimmed to
        `max_tokens`. Neighbours that do not fit are shown as signatures, then dropped."""
        return run(tools.get_context, symbol, depth, max_tokens)

    @server.tool()
    def repo_overview() -> str:
        """Module tree with one line per file: size and a one-line summary."""
        return run(tools.repo_overview)

    @server.tool()
    def reindex() -> str:
        """Re-index changed files now (normally done automatically when files change)."""
        return state.reindex()

    return server


def serve(root: str | Path, out: str | Path | None = None, transport: str = "stdio",
          host: str = "127.0.0.1", port: int = 8000) -> None:
    server = create_server(root, out)
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=host, port=port)
