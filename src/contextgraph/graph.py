"""Build a NetworkX MultiDiGraph from parse results and answer queries over it.

Node ids:
  files      the repo-relative path, e.g. "pkg/mod.py"
  symbols    the qualified name, e.g. "pkg.mod.Class.method"
  externals  "ext:" + dotted name, e.g. "ext:os.path.join"

Edge keys (one edge per pair and type): "defines", "imports", "calls", "inherits".
Call edges carry `lines` (call sites in the caller's file) and `resolution`: "exact" when the
target was found through scopes, imports or `self`, "name" when it was matched by a unique
method/function name because the receiver's type is unknown.
"""

from __future__ import annotations

import builtins
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from contextgraph.models import FileResult, Symbol

EXT = "ext:"
_BUILTINS = set(dir(builtins))
# Method names that are far more likely to hit a list/dict/str/file than a repo method.
_COMMON_METHODS = {
    n
    for t in (list, dict, str, set, bytes, tuple, frozenset)
    for n in dir(t)
    if not n.startswith("_")
} | {"read", "write", "close", "flush", "seek", "readline", "readlines", "open", "send", "recv"}


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token), good enough for budgeting."""
    return (len(text) + 3) // 4


@dataclass
class Edge:
    source: str
    target: str
    kind: str
    lines: list[int]
    resolution: str = "exact"


class CodeGraph:
    def __init__(self, root: str | Path, graph: nx.MultiDiGraph | None = None) -> None:
        self.root = Path(root)
        self.g = graph if graph is not None else nx.MultiDiGraph()
        self._source_cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ build

    @classmethod
    def build(cls, root: str | Path, results: dict[str, FileResult]) -> CodeGraph:
        cg = cls(root)
        _Builder(cg.g, results).run()
        return cg

    # ------------------------------------------------------------- node info

    def node(self, node_id: str) -> dict:
        return {"id": node_id, **self.g.nodes[node_id]}

    def files(self) -> list[str]:
        return sorted(n for n, d in self.g.nodes(data=True) if d["kind"] == "file")

    def symbols(self) -> list[str]:
        return [n for n, d in self.g.nodes(data=True) if d["kind"] in ("class", "function", "method")]

    def stats(self) -> dict:
        kinds: dict[str, int] = {}
        for _, d in self.g.nodes(data=True):
            kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
        edges: dict[str, int] = {}
        for _, _, k in self.g.edges(keys=True):
            edges[k] = edges.get(k, 0) + 1
        return {"nodes": kinds, "edges": edges}

    # --------------------------------------------------------------- lookups

    def resolve_symbol(self, query: str) -> list[str]:
        """Exact qualname, then dotted suffix ("Session.request"), then bare name."""
        q = query.strip()
        if q in self.g and self.g.nodes[q]["kind"] in ("class", "function", "method"):
            return [q]
        syms = self.symbols()
        suffix = [s for s in syms if s.endswith("." + q)]
        if suffix:
            return sorted(suffix)
        lower = q.lower()
        return sorted(s for s in syms if self.g.nodes[s]["name"].lower() == lower)

    def resolve_file(self, query: str) -> list[str]:
        q = query.strip().lstrip("./")
        files = self.files()
        if q in files:
            return [q]
        by_module = [f for f in files if self.g.nodes[f]["module"] == q]
        if by_module:
            return by_module
        return sorted(f for f in files if f.endswith("/" + q) or f.endswith(q))

    def search_symbol(self, query: str, kind: str | None = None, limit: int = 20) -> list[dict]:
        q = query.strip().lower()
        scored = []
        for s in self.symbols():
            d = self.g.nodes[s]
            if kind and d["kind"] != kind:
                continue
            name, qual = d["name"].lower(), s.lower()
            if name == q or qual == q:
                score = 0
            elif qual.endswith("." + q):
                score = 1
            elif name.startswith(q):
                score = 2
            elif q in name:
                score = 3
            elif q in qual:
                score = 4
            else:
                continue
            scored.append((score, len(s), s))
        scored.sort()
        return [self._summary(s) for _, _, s in scored[:limit]]

    def _summary(self, s: str) -> dict:
        d = self.g.nodes[s]
        return {
            "symbol": s,
            "kind": d["kind"],
            "location": f"{d['path']}:{d['start_line']}",
            "signature": d["signature"],
            "doc": d["docstring"].split("\n", 1)[0] if d["docstring"] else "",
        }

    # ---------------------------------------------------------------- source

    def _lines(self, path: str) -> list[str]:
        if path not in self._source_cache:
            try:
                text = (self.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._source_cache[path] = text.splitlines()
        return self._source_cache[path]

    def source(self, symbol: str) -> str:
        d = self.g.nodes[symbol]
        lines = self._lines(d["path"])
        return "\n".join(lines[d["start_line"] - 1 : d["end_line"]])

    def outline(self, symbol: str) -> str:
        """Signature + docstring, and for classes the signatures of their members."""
        d = self.g.nodes[symbol]
        out = [d["signature"] + ":"]
        if d["docstring"]:
            out.append('    """' + d["docstring"].split("\n\n", 1)[0].strip() + '"""')
        if d["kind"] == "class":
            for member in self.members(symbol):
                out.append("    " + self.g.nodes[member]["signature"] + ": ...")
        else:
            out.append("    ...")
        return "\n".join(out)

    def members(self, symbol: str) -> list[str]:
        out = [t for _, t, k in self.g.out_edges(symbol, keys=True) if k == "defines"]
        return sorted(out, key=lambda m: self.g.nodes[m]["start_line"])

    # ----------------------------------------------------------------- calls

    def _edges(self, node: str, kind: str, reverse: bool) -> list[Edge]:
        it = self.g.in_edges(node, keys=True, data=True) if reverse else self.g.out_edges(node, keys=True, data=True)
        out = []
        for s, t, k, d in it:
            if k == kind:
                out.append(Edge(s, t, k, d.get("lines", []), d.get("resolution", "exact")))
        return out

    def find_callers(self, symbol: str, depth: int = 1) -> list[dict]:
        return self._walk_calls(symbol, depth, reverse=True)

    def find_callees(self, symbol: str, depth: int = 1) -> list[dict]:
        return self._walk_calls(symbol, depth, reverse=False)

    def _walk_calls(self, symbol: str, depth: int, reverse: bool) -> list[dict]:
        seen = {symbol}
        frontier = [symbol]
        out = []
        for hop in range(1, max(depth, 1) + 1):
            nxt = []
            for node in frontier:
                for e in sorted(self._edges(node, "calls", reverse), key=lambda e: (e.source, e.target)):
                    other = e.source if reverse else e.target
                    if other in seen:
                        continue
                    seen.add(other)
                    d = self.g.nodes[other]
                    # Call-site lines live in the caller's file.
                    caller = e.source
                    path = self.g.nodes[caller].get("path", "")
                    out.append(
                        {
                            "symbol": other,
                            "kind": d["kind"],
                            "hop": hop,
                            "via": node,
                            "call_sites": [f"{path}:{ln}" for ln in e.lines] if path else [],
                            "resolution": e.resolution,
                        }
                    )
                    if d["kind"] != "external":
                        nxt.append(other)
            frontier = nxt
        return out

    # ----------------------------------------------------------------- files

    def get_dependencies(self, file: str) -> list[dict]:
        out = []
        for e in self._edges(file, "imports", reverse=False):
            d = self.g.nodes[e.target]
            out.append({"target": e.target.removeprefix(EXT), "internal": d["kind"] == "file", "lines": e.lines})
        return sorted(out, key=lambda x: (not x["internal"], x["target"]))

    def get_dependents(self, file: str, depth: int = 1) -> list[dict]:
        seen = {file}
        frontier = [file]
        out = []
        for hop in range(1, max(depth, 1) + 1):
            nxt = []
            for node in frontier:
                for e in self._edges(node, "imports", reverse=True):
                    if e.source in seen:
                        continue
                    seen.add(e.source)
                    out.append({"file": e.source, "hop": hop, "lines": e.lines})
                    nxt.append(e.source)
            frontier = nxt
        return sorted(out, key=lambda x: (x["hop"], x["file"]))

    # --------------------------------------------------------------- context

    def get_context(self, symbol: str, depth: int = 1, max_tokens: int = 4000) -> dict:
        """The symbol's source plus callers/callees up to `depth` hops, trimmed to a token budget.

        Nearer hops go first. Each neighbour gets its full source if it fits, otherwise its
        outline (signature + docstring), otherwise just its name in `omitted`.
        """
        target = self.node(symbol)
        sections: list[dict] = []
        used = 0

        def add(sym: str, role: str, hop: int, allow_outline: bool = True) -> bool:
            nonlocal used
            d = self.g.nodes[sym]
            header = f"{role} {sym} ({d['kind']}) {d['path']}:{d['start_line']}-{d['end_line']}"
            body = self.source(sym)
            cost = estimate_tokens(header + body)
            mode = "source"
            if used + cost > max_tokens and allow_outline:
                body = self.outline(sym)
                cost = estimate_tokens(header + body)
                mode = "outline"
            if used + cost > max_tokens:
                return False
            used += cost
            sections.append({"role": role, "symbol": sym, "hop": hop, "mode": mode, "header": header, "body": body})
            return True

        if not add(symbol, "target", 0):
            # Even the outline does not fit: hard-truncate it.
            body = self.outline(symbol)[: max_tokens * 4]
            sections.append({"role": "target", "symbol": symbol, "hop": 0, "mode": "truncated", "header": symbol, "body": body})
            used = estimate_tokens(body)

        callers = self.find_callers(symbol, depth)
        callees = self.find_callees(symbol, depth)
        externals = sorted(c["symbol"].removeprefix(EXT) for c in callees if c["kind"] == "external")
        module_callers = sorted(c["symbol"] for c in callers if c["kind"] == "file")
        neighbours = [("caller", c) for c in callers if c["kind"] != "file"] + [("callee", c) for c in callees if c["kind"] != "external"]
        neighbours.sort(key=lambda rc: (rc[1]["hop"], rc[1]["resolution"] != "exact", rc[0] != "callee"))
        omitted = []
        for role, c in neighbours:
            if not add(c["symbol"], f"{role}[hop {c['hop']}]", c["hop"]):
                omitted.append(c["symbol"])

        if target["kind"] == "method" and target.get("parent"):
            parent = target["parent"]
            header = f"enclosing class {parent}"
            body = self.g.nodes[parent]["signature"] + ":"
            if used + estimate_tokens(header + body) <= max_tokens:
                sections.insert(1, {"role": "class", "symbol": parent, "hop": 0, "mode": "outline", "header": header, "body": body})
                used += estimate_tokens(header + body)

        return {"symbol": symbol, "tokens": used, "max_tokens": max_tokens, "sections": sections,
                "external_calls": externals, "module_callers": module_callers, "omitted": omitted}

    # -------------------------------------------------------------- overview

    def repo_overview(self) -> list[dict]:
        out = []
        for f in self.files():
            d = self.g.nodes[f]
            if d["docstring"]:
                summary = d["docstring"].strip().split("\n", 1)[0]
            else:
                top = [t for _, t, k in self.g.out_edges(f, keys=True) if k == "defines"]
                top.sort(key=lambda s: self.g.nodes[s]["start_line"])
                names = [self.g.nodes[s]["name"] for s in top]
                summary = ("defines " + ", ".join(names[:6]) + (f" (+{len(names) - 6} more)" if len(names) > 6 else "")) if names else ""
            out.append({"file": f, "lines": d["lines"], "summary": summary})
        return out


class _Builder:
    """Turns per-file parse results into nodes and resolved edges."""

    def __init__(self, g: nx.MultiDiGraph, results: dict[str, FileResult]) -> None:
        self.g = g
        self.results = results
        self.modules: dict[str, str] = {r.module: path for path, r in results.items()}
        self.top_packages = {m.split(".")[0] for m in self.modules if m}
        self.symbols: dict[str, Symbol] = {}
        self.bindings: dict[str, dict[str, str]] = {}  # module -> local name -> dotted target
        self.star: dict[str, list[str]] = {}  # module -> star-imported modules
        self.by_name: dict[str, list[str]] = {}
        self.bases: dict[str, list[str]] = {}  # class qualname -> resolved base ids

    def run(self) -> None:
        for path, r in sorted(self.results.items()):
            self.g.add_node(path, kind="file", path=path, module=r.module, language=r.language,
                            lines=r.line_count, docstring=r.docstring)
            for s in r.symbols:
                if s.qualname in self.symbols:
                    continue  # property setters and conditional redefinitions keep the first one
                self.symbols[s.qualname] = s
                self.g.add_node(s.qualname, kind=s.kind, name=s.name, path=path, start_line=s.start_line,
                                end_line=s.end_line, signature=s.signature, docstring=s.docstring,
                                parent=s.parent, decorators=s.decorators)
                self.g.add_edge(s.parent or path, s.qualname, key="defines")
                if s.kind in ("function", "method"):
                    self.by_name.setdefault(s.name, []).append(s.qualname)
            self.bindings[r.module] = self._bindings(r)
        for path, r in self.results.items():
            self._imports(path, r)
        for r in self.results.values():
            for s in r.symbols:
                if s.kind == "class" and self.symbols.get(s.qualname) is s:
                    self._inherits(r, s)
        for path, r in self.results.items():
            self._calls(path, r)

    # -------------------------------------------------------------- bindings

    def _bindings(self, r: FileResult) -> dict[str, str]:
        b: dict[str, str] = {}
        stars = []
        for imp in r.imports:
            if imp.name is None:
                b[imp.alias] = imp.module
                if imp.alias == imp.module and "." in imp.module:
                    b.setdefault(imp.module.split(".")[0], imp.module.split(".")[0])
            elif imp.name == "*":
                stars.append(imp.module)
            else:
                b[imp.alias] = f"{imp.module}.{imp.name}" if imp.module else imp.name
        for s in r.symbols:
            if s.parent is None:
                b[s.name] = s.qualname
        self.star[r.module] = stars
        return b

    def _ext(self, dotted: str) -> str:
        node = EXT + dotted
        if node not in self.g:
            self.g.add_node(node, kind="external", name=dotted, path="")
        return node

    def _is_internal(self, dotted: str) -> bool:
        return dotted.split(".")[0] in self.top_packages

    # ------------------------------------------------------------ resolution

    def resolve(self, dotted: str, _depth: int = 0) -> str | None:
        """Resolve an absolute dotted name to a symbol id or file path inside the repo."""
        if _depth > 8 or not dotted:
            return None
        if dotted in self.symbols:
            return dotted
        if dotted in self.modules:
            return self.modules[dotted]
        parts = dotted.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod = ".".join(parts[:i])
            if mod not in self.modules:
                continue
            rest = parts[i:]
            head = f"{mod}.{rest[0]}"
            if head in self.symbols:
                return self._attr_chain(head, rest[1:])
            # Re-export: `from .sessions import Session` inside pkg/__init__.py
            target = self.bindings.get(mod, {}).get(rest[0])
            if target and target != head:
                return self.resolve(".".join([target, *rest[1:]]), _depth + 1)
            for star in self.star.get(mod, []):
                found = self.resolve(".".join([star, *rest]), _depth + 1)
                if found:
                    return found
            return None
        return None

    def _attr_chain(self, sym: str, attrs: list[str]) -> str | None:
        for a in attrs:
            nxt = self.member(sym, a)
            if nxt is None:
                return None
            sym = nxt
        return sym

    def member(self, cls: str, attr: str, _seen: set[str] | None = None) -> str | None:
        seen = _seen or set()
        if cls in seen:
            return None
        seen.add(cls)
        cand = f"{cls}.{attr}"
        if cand in self.symbols:
            return cand
        for base in self.bases.get(cls, []):
            if base in self.symbols:
                found = self.member(base, attr, seen)
                if found:
                    return found
        return None

    def _enclosing_class(self, scope: str | None) -> str | None:
        while scope:
            s = self.symbols.get(scope)
            if s is None:
                return None
            if s.kind == "class":
                return scope
            scope = s.parent
        return None

    def _resolve_in_scope(self, module: str, scope: str | None, dotted: str) -> tuple[str | None, str]:
        """Resolve a call/base expression seen in `scope` of `module`.

        Returns (node id or None, resolution). External targets come back as ext: ids.
        Values are tracked as ("sym", qualname), ("inst", class qualname), ("ext", dotted).
        """
        parts = dotted.split(".")
        value = self._head(module, scope, parts[0])
        i = 1
        if value is not None and value[0] == "binding":
            # Longest dotted prefix bound by an import, e.g. `import os.path` then os.path.join()
            binds = self.bindings.get(module, {})
            plain = [p.removesuffix("()") for p in parts]
            j = len(parts)
            while j > 1 and ".".join(plain[:j]) not in binds:
                j -= 1
            head = binds.get(".".join(plain[:j]), value[1])
            if not self._is_internal(head):
                return self._ext(".".join([head, *plain[j:]])), "exact"
            value, i = self._lookup(head), j
            if value is not None and parts[j - 1].endswith("()"):
                value = self._call(value)
        for k, part in enumerate(parts[i:], start=i):
            if value is None:
                break
            value = self._step(value, part, last=k == len(parts) - 1)
        if value is not None:
            kind, ident = value
            if kind == "sym" and self.g.has_node(ident) and self.g.nodes[ident]["kind"] != "file":
                return ident, "exact"
            if kind == "ext":
                return self._ext(ident), "exact"
            if kind == "sym" or kind == "inst":
                return None, ""
        # Unknown receiver: fall back to a unique function/method name in the repo.
        name = parts[-1].removesuffix("()")
        if len(parts) > 1 and name not in _COMMON_METHODS:
            cands = self.by_name.get(name, [])
            if len(cands) == 1:
                return cands[0], "name"
        return None, ""

    def _lookup(self, full: str) -> tuple[str, str] | None:
        found = self.resolve(full)
        if found is None:
            return None
        return ("sym", found) if found in self.symbols else ("mod", self.g.nodes[found]["module"])

    def _head(self, module: str, scope: str | None, head: str):
        call = head.endswith("()")
        name = head.removesuffix("()")
        if name in ("self", "cls"):
            cls = self._enclosing_class(scope)
            return ("inst", cls) if cls else None
        if name == "super":
            cls = self._enclosing_class(scope)
            return ("super", cls) if cls else None
        if name == "?":
            return None
        value = None
        s = scope
        while s and value is None:
            sym = self.symbols.get(s)
            if sym is None:
                break
            if sym.kind != "class":
                if name in sym.locals:
                    value = self._typed(module, sym.parent, sym.locals[name])
                    if value is None:
                        return None
                    break
                local = f"{s}.{name}"
                if local in self.symbols and self.symbols[local].kind != "method":
                    value = ("sym", local)
            s = sym.parent
        if value is None:
            binds = self.bindings.get(module, {})
            if name in binds or any(k.startswith(name + ".") for k in binds):
                target = binds.get(name, name)
                if not call:
                    return ("binding", target)
                value = self._lookup(target) if self._is_internal(target) else ("ext", target)
            elif name in _BUILTINS:
                return None
            else:
                for star in self.star.get(module, []):
                    found = self.resolve(f"{star}.{name}")
                    if found in self.symbols:
                        value = ("sym", found)
                        break
        if value is not None and call:
            value = self._call(value)
        return value

    def _typed(self, module: str, scope: str | None, spec: str):
        """Turn a recorded local ("type:Foo" / "call:make()") into a value."""
        kind, _, expr = spec.partition(":")
        if kind == "call":
            parts = expr.split(".")
            value = self._head(module, scope, parts[0])
            if value is not None and value[0] == "binding":
                value = self._lookup(value[1]) if self._is_internal(value[1]) else ("ext", value[1])
            for part in parts[1:]:
                if value is None:
                    return None
                value = self._step(value, part, last=False)
            return value
        cls = self._type_name(module, expr)
        return ("inst", cls) if cls else None

    def _type_name(self, module: str, annotation: str) -> str | None:
        """First class in an annotation such as `Catalog`, `"Catalog"`, `Catalog | None`."""
        for name in re.findall(r"[A-Za-z_][\w.]*", annotation):
            if name in ("Optional", "Union", "None", "List", "list", "Type", "type", "typing"):
                continue
            target = self.bindings.get(module, {}).get(name.split(".")[0])
            full = ".".join([target, *name.split(".")[1:]]) if target else f"{module}.{name}"
            found = self.resolve(full)
            if found in self.symbols and self.symbols[found].kind == "class":
                return found
        return None

    def _call(self, value):
        kind, ident = value
        if kind == "sym":
            sym = self.symbols.get(ident)
            if sym is None:
                return None
            if sym.kind == "class":
                return ("inst", ident)
            if sym.returns:
                mod = self.g.nodes[ident]["path"]
                cls = self._type_name(self.results[mod].module, sym.returns)
                return ("inst", cls) if cls else None
            return None
        if kind == "ext":
            return ("ext", ident + "()")
        return None

    def _step(self, value, part: str, last: bool):
        call = part.endswith("()")
        name = part.removesuffix("()")
        kind, ident = value
        nxt = None
        if kind == "super":
            for base in self.bases.get(ident, []):
                if base in self.symbols:
                    found = self.member(base, name)
                    if found:
                        nxt = ("sym", found)
                        break
        elif kind in ("inst", "sym") and ident in self.symbols:
            found = self.member(ident, name)
            if found:
                nxt = ("sym", found)
            elif kind == "inst":
                spec = self._attr_type(ident, "self." + name)
                if spec:
                    cls_mod = self.results[self.g.nodes[ident]["path"]].module
                    nxt = self._typed(cls_mod, self.symbols[ident].parent, spec)
        elif kind == "mod":
            nxt = self._lookup(f"{ident}.{name}")
        elif kind == "ext":
            nxt = ("ext", f"{ident.removesuffix('()')}.{name}" if not ident.endswith("()") else f"{ident}.{name}")
        if nxt is not None and call and not last:
            nxt = self._call(nxt)
        return nxt

    def _attr_type(self, cls: str, key: str, _seen: set[str] | None = None) -> str | None:
        seen = _seen or set()
        if cls in seen or cls not in self.symbols:
            return None
        seen.add(cls)
        if key in self.symbols[cls].locals:
            return self.symbols[cls].locals[key]
        for base in self.bases.get(cls, []):
            found = self._attr_type(base, key, seen)
            if found:
                return found
        return None


    # ----------------------------------------------------------------- edges

    def _add(self, source: str, target: str, kind: str, line: int, resolution: str = "exact") -> None:
        if source == target:
            return
        if self.g.has_edge(source, target, key=kind):
            data = self.g.edges[source, target, kind]
            if line not in data["lines"]:
                data["lines"].append(line)
            if resolution == "exact":
                data["resolution"] = "exact"
        else:
            self.g.add_edge(source, target, key=kind, lines=[line], resolution=resolution)

    def _imports(self, path: str, r: FileResult) -> None:
        for imp in r.imports:
            target = None
            if imp.name not in (None, "*"):
                sub = f"{imp.module}.{imp.name}" if imp.module else imp.name
                target = self.modules.get(sub)
            if target is None:
                target = self.modules.get(imp.module)
            if target is None:
                if self._is_internal(imp.module) or not imp.module:
                    continue
                target = self._ext(imp.module)
            self._add(path, target, "imports", imp.line)

    def _inherits(self, r: FileResult, s: Symbol) -> None:
        resolved = []
        for base in s.bases:
            target, _ = self._resolve_in_scope(r.module, s.parent, base)
            if target is None and not self._is_internal(base) and base.split(".")[0] not in self.bindings.get(r.module, {}):
                target = self._ext(base)
            if target is None or self.g.nodes[target]["kind"] == "file":
                continue
            resolved.append(target)
            self._add(s.qualname, target, "inherits", s.start_line)
        self.bases[s.qualname] = resolved

    def _calls(self, path: str, r: FileResult) -> None:
        for c in r.calls:
            target, how = self._resolve_in_scope(r.module, c.caller, c.callee)
            if target is None or self.g.nodes[target]["kind"] == "file":
                continue
            source = c.caller if c.caller in self.symbols else path
            self._add(source, target, "calls", c.line, how)
