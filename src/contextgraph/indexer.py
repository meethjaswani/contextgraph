"""Index a repo to disk and load it back. Re-indexing only re-parses files whose hash changed."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from contextgraph import __version__
from contextgraph.graph import CodeGraph
from contextgraph.models import FileResult
from contextgraph.parsers import parser_for, supported_extensions

INDEX_DIR = ".contextgraph"
INDEX_FILE = "index.json"
GRAPH_FILE = "graph.json"
SCHEMA = 1

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", "build", "dist",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs", "site-packages", INDEX_DIR,
}


@dataclass
class IndexStats:
    files: int = 0
    parsed: int = 0
    reused: int = 0
    removed: int = 0
    failed: list[str] = field(default_factory=list)
    seconds: float = 0.0


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover(root: Path) -> list[str]:
    """Source files in the repo. Uses `git ls-files` when possible so .gitignore is respected."""
    exts = supported_extensions()
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        paths = [p for p in out.splitlines() if p.endswith(exts)]
        paths = [p for p in paths if not _SKIP_DIRS.intersection(p.split("/")[:-1]) and (root / p).is_file()]
        if paths:
            return sorted(paths)
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(exts):
                found.append(Path(dirpath, name).relative_to(root).as_posix())
    return sorted(found)


def index_dir(root: Path, out: Path | None = None) -> Path:
    return out if out is not None else root / INDEX_DIR


def _load_cache(path: Path) -> dict:
    try:
        data = json.loads((path / INDEX_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if data.get("schema") != SCHEMA:
        return {}
    return data.get("files", {})


def index_repo(root: str | Path, out: str | Path | None = None, force: bool = False) -> tuple[CodeGraph, IndexStats]:
    root = Path(root).resolve()
    target = index_dir(root, Path(out) if out else None)
    started = time.perf_counter()
    cache = {} if force else _load_cache(target)
    stats = IndexStats()
    files: dict[str, dict] = {}
    results: dict[str, FileResult] = {}
    for rel in discover(root):
        parser = parser_for(rel)
        if parser is None:
            continue
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        digest = file_hash(data)
        cached = cache.get(rel)
        if cached and cached.get("hash") == digest:
            result = FileResult.from_dict(cached["result"])
            stats.reused += 1
        else:
            try:
                result = parser.parse(rel, data)
            except Exception:  # a broken file should not stop the index
                stats.failed.append(rel)
                continue
            stats.parsed += 1
        files[rel] = {"hash": digest, "result": result.to_dict()}
        results[rel] = result
    stats.files = len(files)
    stats.removed = len(set(cache) - set(files))
    cg = CodeGraph.build(root, results)
    save(target, files, cg)
    stats.seconds = round(time.perf_counter() - started, 3)
    return cg, stats


def save(target: Path, files: dict[str, dict], cg: CodeGraph) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / INDEX_FILE).write_text(json.dumps({"schema": SCHEMA, "version": __version__, "files": files}))
    data = nx.node_link_data(cg.g, edges="edges")
    (target / GRAPH_FILE).write_text(json.dumps(data))


def load(root: str | Path, out: str | Path | None = None) -> CodeGraph:
    root = Path(root).resolve()
    target = index_dir(root, Path(out) if out else None)
    data = json.loads((target / GRAPH_FILE).read_text())
    g = nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")
    return CodeGraph(root, g)


def load_or_index(root: str | Path, out: str | Path | None = None) -> CodeGraph:
    """Refresh the index (cheap when nothing changed) and return the graph."""
    cg, _ = index_repo(root, out)
    return cg
