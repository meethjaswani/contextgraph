from contextgraph.indexer import index_repo, load


def test_reindex_reuses_unchanged_files(repo):
    _, first = index_repo(repo)
    assert first.parsed == 10 and first.reused == 0
    _, second = index_repo(repo)
    assert second.parsed == 0 and second.reused == 10


def test_changed_and_removed_files(repo):
    index_repo(repo)
    pricing = repo / "bookshop" / "pricing.py"
    pricing.write_text(pricing.read_text() + "\n\ndef shipping(lines):\n    return total(lines) + 5\n")
    (repo / "tests" / "test_orders.py").unlink()
    cg, stats = index_repo(repo)
    assert stats.parsed == 1 and stats.reused == 8 and stats.removed == 1
    assert {r["symbol"] for r in cg.find_callers("bookshop.pricing.total")} >= {"bookshop.pricing.shipping"}


def test_saved_graph_loads(repo):
    cg, _ = index_repo(repo)
    loaded = load(repo)
    assert loaded.stats() == cg.stats()
    assert loaded.g.edges["bookshop.orders.quote", "bookshop.pricing.total", "calls"]["lines"] == [32]


def test_force_reparses(repo):
    index_repo(repo)
    _, stats = index_repo(repo, force=True)
    assert stats.parsed == 10
