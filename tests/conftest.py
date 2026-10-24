import shutil
from pathlib import Path

import pytest

from contextgraph.indexer import index_repo

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "bookshop"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A fresh copy of the sample repo, so tests can edit files."""
    dst = tmp_path / "bookshop"
    shutil.copytree(SAMPLE, dst, ignore=shutil.ignore_patterns(".contextgraph", "__pycache__"))
    return dst


@pytest.fixture()
def cg(repo: Path):
    graph, _ = index_repo(repo)
    return graph
