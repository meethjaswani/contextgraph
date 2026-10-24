"""Plain data records produced by parsers and consumed by the graph builder."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Symbol:
    """A class, function or method defined in a file."""

    qualname: str  # e.g. "pkg.mod.Class.method"
    name: str
    kind: str  # "class" | "function" | "method"
    start_line: int  # 1-based, inclusive
    end_line: int
    signature: str
    docstring: str = ""
    parent: str | None = None  # qualname of the enclosing class/function, None at module level
    bases: list[str] = field(default_factory=list)  # raw base-class expressions, classes only
    decorators: list[str] = field(default_factory=list)
    returns: str = ""  # return annotation, functions only
    # Local names with a known type, used to resolve `x.method()`:
    # "type:Catalog" for annotations, "call:make_catalog()" for `x = make_catalog()`.
    # Classes hold instance attributes as "self.attr".
    locals: dict[str, str] = field(default_factory=dict)


@dataclass
class Import:
    """One imported name. `import a.b as c` -> module="a.b", name=None, alias="c"."""

    module: str  # absolute dotted module (relative imports are resolved by the parser)
    name: str | None
    alias: str
    line: int


@dataclass
class Call:
    """A call site. `callee` is the raw dotted expression, e.g. "self.save" or "os.path.join"."""

    caller: str | None  # qualname of the enclosing symbol, None for module-level code
    callee: str
    line: int


@dataclass
class FileResult:
    path: str  # repo-relative, forward slashes
    module: str  # dotted module name
    language: str
    line_count: int
    docstring: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> FileResult:
        return cls(
            path=d["path"],
            module=d["module"],
            language=d["language"],
            line_count=d["line_count"],
            docstring=d.get("docstring", ""),
            symbols=[Symbol(**s) for s in d["symbols"]],
            imports=[Import(**i) for i in d["imports"]],
            calls=[Call(**c) for c in d["calls"]],
        )
