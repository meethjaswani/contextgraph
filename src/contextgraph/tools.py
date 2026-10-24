"""Tool implementations shared by the MCP server and the CLI. Each returns compact plain text."""

from __future__ import annotations

from contextgraph.graph import EXT, CodeGraph


class ToolError(Exception):
    pass


def _one_symbol(cg: CodeGraph, symbol: str) -> str:
    matches = cg.resolve_symbol(symbol)
    if not matches:
        hits = cg.search_symbol(symbol, limit=5)
        hint = ("; did you mean: " + ", ".join(h["symbol"] for h in hits)) if hits else ""
        raise ToolError(f"no symbol named {symbol!r}{hint}")
    if len(matches) > 1:
        lines = [f"{symbol!r} is ambiguous, pass one of these qualified names:"]
        lines += [f"  {m}  ({cg.g.nodes[m]['kind']}, {cg.g.nodes[m]['path']}:{cg.g.nodes[m]['start_line']})" for m in matches[:15]]
        raise ToolError("\n".join(lines))
    return matches[0]


def _one_file(cg: CodeGraph, file: str) -> str:
    matches = cg.resolve_file(file)
    if not matches:
        raise ToolError(f"no indexed file matches {file!r}")
    if len(matches) > 1:
        raise ToolError(f"{file!r} is ambiguous: " + ", ".join(matches[:15]))
    return matches[0]


def search_symbol(cg: CodeGraph, query: str, kind: str | None = None, limit: int = 20) -> str:
    hits = cg.search_symbol(query, kind=kind, limit=limit)
    if not hits:
        return f"no symbols match {query!r}"
    lines = []
    for h in hits:
        doc = f"  # {h['doc']}" if h["doc"] else ""
        lines.append(f"{h['symbol']}  [{h['kind']}] {h['location']}\n    {h['signature']}{doc}")
    return "\n".join(lines)


def get_definition(cg: CodeGraph, symbol: str) -> str:
    s = _one_symbol(cg, symbol)
    d = cg.g.nodes[s]
    return f"# {s} ({d['kind']}) {d['path']}:{d['start_line']}-{d['end_line']}\n{cg.source(s)}"


def _calls_text(cg: CodeGraph, s: str, rows: list[dict], label: str, limit: int = 60) -> str:
    if not rows:
        return f"no {label} found for {s}"
    lines = [f"{label} of {s}:"]
    for r in rows[:limit]:
        name = r["symbol"].removeprefix(EXT)
        node = cg.g.nodes[r["symbol"]]
        where = f"{node['path']}:{node['start_line']}" if node["kind"] not in ("external", "file") else node["kind"]
        if node["kind"] == "file":
            name = f"<module {name}>"
        sites = ", ".join(r["call_sites"][:5]) + (" ..." if len(r["call_sites"]) > 5 else "")
        guess = " (matched by name)" if r["resolution"] == "name" else ""
        hop = f"[hop {r['hop']}] " if r["hop"] > 1 else ""
        lines.append(f"  {hop}{name}  defined {where}; call sites: {sites}{guess}")
    if len(rows) > limit:
        lines.append(f"  ... {len(rows) - limit} more")
    return "\n".join(lines)


def find_callers(cg: CodeGraph, symbol: str, depth: int = 1) -> str:
    s = _one_symbol(cg, symbol)
    return _calls_text(cg, s, cg.find_callers(s, depth), "callers")


def find_callees(cg: CodeGraph, symbol: str, depth: int = 1) -> str:
    s = _one_symbol(cg, symbol)
    return _calls_text(cg, s, cg.find_callees(s, depth), "callees")


def get_dependencies(cg: CodeGraph, file: str) -> str:
    f = _one_file(cg, file)
    deps = cg.get_dependencies(f)
    if not deps:
        return f"{f} imports nothing"
    internal = [d["target"] for d in deps if d["internal"]]
    external = [d["target"] for d in deps if not d["internal"]]
    return f"{f} imports\n  in repo: {', '.join(internal) or '-'}\n  external: {', '.join(external) or '-'}"


def get_dependents(cg: CodeGraph, file: str, depth: int = 1) -> str:
    f = _one_file(cg, file)
    rows = cg.get_dependents(f, depth)
    if not rows:
        return f"no files import {f}"
    lines = [f"files that import {f}:"]
    lines += [f"  {'[hop %d] ' % r['hop'] if r['hop'] > 1 else ''}{r['file']}" for r in rows]
    return "\n".join(lines)


def get_context(cg: CodeGraph, symbol: str, depth: int = 1, max_tokens: int = 4000) -> str:
    s = _one_symbol(cg, symbol)
    ctx = cg.get_context(s, depth=depth, max_tokens=max_tokens)
    parts = [f"# context for {s}: ~{ctx['tokens']} tokens (budget {max_tokens}), depth {depth}"]
    for sec in ctx["sections"]:
        note = " [outline only]" if sec["mode"] == "outline" else ""
        parts.append(f"## {sec['header']}{note}\n{sec['body']}")
    if ctx["external_calls"]:
        parts.append("## external calls\n" + ", ".join(ctx["external_calls"]))
    if ctx["module_callers"]:
        parts.append("## called at module level in\n" + ", ".join(ctx["module_callers"]))
    if ctx["omitted"]:
        parts.append("## omitted (over budget)\n" + ", ".join(ctx["omitted"]))
    return "\n\n".join(parts)


def repo_overview(cg: CodeGraph) -> str:
    rows = cg.repo_overview()
    stats = cg.stats()
    n = stats["nodes"]
    head = (f"{len(rows)} files, {n.get('class', 0)} classes, {n.get('function', 0)} functions, "
            f"{n.get('method', 0)} methods")
    lines = [head]
    for r in rows:
        depth = r["file"].count("/")
        lines.append(f"{'  ' * depth}{r['file']} ({r['lines']} lines){' - ' + r['summary'] if r['summary'] else ''}")
    return "\n".join(lines)
