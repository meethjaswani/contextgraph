import pytest

from contextgraph import tools


def names(rows):
    return {r["symbol"] for r in rows}


def test_nodes_and_edge_types(cg):
    stats = cg.stats()
    assert stats["nodes"]["file"] == 10
    assert set(stats["edges"]) == {"defines", "imports", "calls", "inherits"}
    assert "bookshop/orders.py" in cg.files()


def test_resolve_symbol_by_suffix_and_name(cg):
    assert cg.resolve_symbol("Catalog.find") == ["bookshop.catalog.Catalog.find"]
    assert cg.resolve_symbol("place_order") == ["bookshop.orders.place_order"]
    assert len(cg.resolve_symbol("_send")) == 3


def test_search_symbol_ranks_exact_first(cg):
    hits = cg.search_symbol("total")
    assert hits[0]["symbol"] == "bookshop.pricing.total"
    assert all(h["kind"] == "class" for h in cg.search_symbol("gateway", kind="class"))


def test_definition_source(cg):
    src = cg.source("bookshop.pricing.with_tax")
    assert src.startswith("def with_tax")
    assert "TAX_RATE" in src


def test_callers_across_modules_and_reexports(cg):
    # cli imports place_order through bookshop/__init__.py
    assert names(cg.find_callers("bookshop.orders.place_order")) == {"bookshop.cli.main"}
    assert names(cg.find_callers("bookshop.pricing.total")) == {"bookshop.orders.place_order", "bookshop.orders.quote"}


def test_self_calls_and_typed_parameters(cg):
    assert names(cg.find_callers("bookshop.catalog.Catalog.find")) == {
        "bookshop.catalog.Catalog.reserve",  # self.find
        "bookshop.orders.quote",  # catalog: Catalog -> catalog.find
    }


def test_return_annotation_resolves_method(cg):
    callers = cg.find_callers("bookshop.payments.gateway.Gateway.charge")
    assert names(callers) == {"bookshop.orders.place_order"}
    assert callers[0]["resolution"] == "exact"


def test_callers_depth(cg):
    rows = cg.find_callers("bookshop.pricing.with_tax", depth=3)
    by_hop = {r["symbol"]: r["hop"] for r in rows}
    assert by_hop["bookshop.pricing.total"] == 1
    assert by_hop["bookshop.orders.place_order"] == 2
    assert by_hop["bookshop.cli.main"] == 3


def test_external_calls_are_leaves(cg):
    callees = cg.find_callees("bookshop.notify.send_receipt")
    assert {"ext:smtplib.SMTP", "bookshop.config.load_settings"} <= names(callees)
    assert cg.g.nodes["ext:smtplib.SMTP"]["kind"] == "external"
    assert not list(cg.g.successors("ext:smtplib.SMTP"))


def test_builtins_are_not_recorded(cg):
    assert not any(n in cg.g for n in ("ext:len", "ext:sum", "ext:round"))


def test_inheritance(cg):
    bases = [t for _, t, k in cg.g.out_edges("bookshop.payments.gateway.CardGateway", keys=True) if k == "inherits"]
    assert bases == ["bookshop.payments.gateway.Gateway"]
    assert "ext:Exception" in cg.g.successors("bookshop.payments.gateway.PaymentError")


def test_dependencies_and_dependents(cg):
    deps = cg.get_dependencies("bookshop/orders.py")
    internal = {d["target"] for d in deps if d["internal"]}
    assert internal == {"bookshop/catalog.py", "bookshop/notify.py", "bookshop/payments/__init__.py", "bookshop/pricing.py"}
    assert {"target": "dataclasses", "internal": False, "lines": [3]} in deps
    direct = {d["file"] for d in cg.get_dependents("bookshop/config.py")}
    assert direct == {"bookshop/notify.py", "bookshop/pricing.py"}
    deep = {d["file"] for d in cg.get_dependents("bookshop/config.py", depth=5)}
    assert {"bookshop/orders.py", "bookshop/__init__.py", "bookshop/cli.py"} <= deep


def test_context_respects_budget(cg):
    ctx = cg.get_context("bookshop.orders.place_order", depth=1, max_tokens=300)
    assert ctx["tokens"] <= 300
    assert ctx["sections"][0]["symbol"] == "bookshop.orders.place_order"
    assert ctx["sections"][0]["mode"] == "source"
    big = cg.get_context("bookshop.orders.place_order", depth=2, max_tokens=10_000)
    assert len(big["sections"]) > len(ctx["sections"])
    assert not big["omitted"]
    assert "smtplib.SMTP" in big["external_calls"]  # reached through send_receipt at hop 2
    assert big["module_callers"] == ["bookshop/cli.py"]  # `main()` under __main__


def test_context_falls_back_to_outlines(cg):
    ctx = cg.get_context("bookshop.orders.place_order", depth=1, max_tokens=420)
    modes = {s["mode"] for s in ctx["sections"][1:]}
    assert "outline" in modes or ctx["omitted"]


def test_overview_lists_every_file(cg):
    rows = cg.repo_overview()
    assert len(rows) == 10
    config = next(r for r in rows if r["file"] == "bookshop/config.py")
    assert config["summary"] == "Settings read from the environment."
    cli = next(r for r in rows if r["file"] == "bookshop/cli.py")
    assert cli["summary"] == "defines build_demo_catalog, main"


def test_tool_text_and_errors(cg):
    assert "bookshop/orders.py:23" in tools.find_callers(cg, "total")
    with pytest.raises(tools.ToolError, match="ambiguous"):
        tools.get_definition(cg, "_send")
    with pytest.raises(tools.ToolError, match="did you mean"):
        tools.get_definition(cg, "plac")
    assert "bookshop/" in tools.repo_overview(cg)
