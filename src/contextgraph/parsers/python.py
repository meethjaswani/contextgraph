"""Python parser built on tree-sitter-python."""

from __future__ import annotations

import ast

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from contextgraph.models import Call, FileResult, Import, Symbol
from contextgraph.parsers.base import LanguageParser

_LANGUAGE = Language(tree_sitter_python.language())
_MAX_DOC = 1000


def _text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _dotted(node: Node | None) -> str | None:
    """Turn `a.b.c` (identifier/attribute chain) into a string. Other receivers become `?`."""
    if node is None:
        return None
    if node.type == "identifier":
        return _text(node)
    if node.type == "attribute":
        attr = node.child_by_field_name("attribute")
        obj = node.child_by_field_name("object")
        if attr is None:
            return None
        if obj is not None and obj.type == "call":
            fn = obj.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" and _text(fn) == "super":
                return f"super.{_text(attr)}"
            inner = _dotted(fn)
            base = f"{inner}()" if inner and "?" not in inner else "?"
            return f"{base}.{_text(attr)}"
        base = _dotted(obj) or "?"
        return f"{base}.{_text(attr)}"
    return None


def _docstring(body: Node | None) -> str:
    if body is None:
        return ""
    for child in body.named_children:
        if child.type == "comment":
            continue
        if child.type == "expression_statement" and child.named_children and child.named_children[0].type == "string":
            raw = _text(child.named_children[0])
            try:
                value = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                value = raw.strip("\"'")
            return value.strip()[:_MAX_DOC] if isinstance(value, str) else ""
        break
    return ""


def _signature(node: Node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    sig = source[node.start_byte : end].decode("utf-8", errors="replace").rstrip().rstrip(":")
    return " ".join(sig.split())


class PythonParser(LanguageParser):
    language = "python"
    extensions = (".py",)

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def module_name(self, path: str) -> str:
        parts = path[: -len(".py")].split("/") if path.endswith(".py") else path.split("/")
        if parts and parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def parse(self, path: str, source: bytes) -> FileResult:
        tree = self._parser.parse(source)
        module = self.module_name(path)
        result = FileResult(
            path=path,
            module=module,
            language=self.language,
            line_count=source.count(b"\n") + (0 if source.endswith(b"\n") or not source else 1),
            docstring=_docstring(tree.root_node),
        )
        package = module if path.endswith("__init__.py") else module.rpartition(".")[0]
        _Walker(result, source, module, package).visit(tree.root_node, None, None)
        return result


class _Walker:
    def __init__(self, result: FileResult, source: bytes, module: str, package: str) -> None:
        self.result = result
        self.source = source
        self.module = module
        self.package = package

    def visit(self, node: Node, scope: Symbol | None, decorators: list[str] | None) -> None:
        t = node.type
        if t == "decorated_definition":
            decs = [_text(d)[1:].strip() for d in node.named_children if d.type == "decorator"]
            definition = node.child_by_field_name("definition")
            for d in node.named_children:
                if d.type == "decorator":
                    self.visit(d, scope, None)
            if definition is not None:
                self.visit(definition, scope, decs)
            return
        if t in ("function_definition", "class_definition"):
            self._definition(node, scope, decorators or [])
            return
        if t == "import_statement":
            self._import(node)
        elif t == "import_from_statement":
            self._import_from(node)
        elif t == "assignment" and scope is not None:
            self._assignment(node, scope)
        elif t == "call":
            callee = _dotted(node.child_by_field_name("function"))
            if callee:
                self.result.calls.append(
                    Call(caller=scope.qualname if scope else None, callee=callee, line=node.start_point[0] + 1)
                )
        for child in node.children:
            self.visit(child, scope, None)

    def _definition(self, node: Node, scope: Symbol | None, decorators: list[str]) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(name_node)
        prefix = scope.qualname if scope else self.module
        is_class = node.type == "class_definition"
        if is_class:
            kind = "class"
        else:
            kind = "method" if scope is not None and scope.kind == "class" else "function"
        bases: list[str] = []
        if is_class:
            supers = node.child_by_field_name("superclasses")
            if supers is not None:
                for arg in supers.named_children:
                    if arg.type == "keyword_argument":
                        continue
                    base = _dotted(arg)
                    if base:
                        bases.append(base)
        body = node.child_by_field_name("body")
        start_node = node.parent if node.parent is not None and node.parent.type == "decorated_definition" else node
        sym = Symbol(
            qualname=f"{prefix}.{name}" if prefix else name,
            name=name,
            kind=kind,
            start_line=start_node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=_signature(node, self.source),
            docstring=_docstring(body),
            parent=scope.qualname if scope else None,
            bases=bases,
            decorators=decorators,
        )
        if not is_class:
            ret = node.child_by_field_name("return_type")
            sym.returns = _text(ret) if ret is not None else ""
            params = node.child_by_field_name("parameters")
            for p in params.named_children if params is not None else []:
                if p.type in ("typed_parameter", "typed_default_parameter"):
                    pname = p.child_by_field_name("name") or (p.named_children[0] if p.named_children else None)
                    ptype = p.child_by_field_name("type")
                    if pname is not None and pname.type == "identifier" and ptype is not None:
                        sym.locals[_text(pname)] = "type:" + _text(ptype)
        self.result.symbols.append(sym)
        # Default values, base classes and annotations run in the enclosing scope.
        for field in ("parameters", "superclasses", "return_type"):
            child = node.child_by_field_name(field)
            if child is not None:
                self.visit(child, scope, None)
        if body is not None:
            self.visit(body, sym, None)

    def _assignment(self, node: Node, scope: Symbol) -> None:
        """Record `x = Foo()`, `x: Foo = ...` and `self.x = Foo()` so later calls on x resolve."""
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        annot = node.child_by_field_name("type")
        if left is None:
            return
        value = None
        if annot is not None:
            value = "type:" + _text(annot)
        elif right is not None and right.type == "call":
            fn = _dotted(right.child_by_field_name("function"))
            if fn and "?" not in fn:
                value = f"call:{fn}()"
        elif right is not None and right.type == "identifier":
            value = scope.locals.get(_text(right))
        if not value:
            return
        if left.type == "identifier":
            scope.locals.setdefault(_text(left), value)
        elif left.type == "attribute" and scope.kind == "method" and scope.parent:
            obj = left.child_by_field_name("object")
            attr = left.child_by_field_name("attribute")
            if obj is not None and attr is not None and _text(obj) == "self":
                cls = next((s for s in self.result.symbols if s.qualname == scope.parent), None)
                if cls is not None:
                    cls.locals.setdefault("self." + _text(attr), value)

    def _absolute(self, relative: Node) -> str:
        """Resolve a `relative_import` node (`..pkg.mod`) against the current package."""
        level = 0
        tail = ""
        for child in relative.children:
            if child.type == "import_prefix":
                level = _text(child).count(".")
            elif child.type == "dotted_name":
                tail = _text(child)
        base = self.package.split(".") if self.package else []
        if level > 1:
            base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
        parts = [p for p in base if p] + ([tail] if tail else [])
        return ".".join(parts)

    def _import(self, node: Node) -> None:
        line = node.start_point[0] + 1
        for child in node.named_children:
            if child.type == "dotted_name":
                mod = _text(child)
                # `import a.b` binds `a`; we record the full module so `a.b.f()` resolves.
                self.result.imports.append(Import(module=mod, name=None, alias=mod, line=line))
            elif child.type == "aliased_import":
                mod = _text(child.child_by_field_name("name"))
                alias = _text(child.child_by_field_name("alias"))
                self.result.imports.append(Import(module=mod, name=None, alias=alias, line=line))

    def _import_from(self, node: Node) -> None:
        line = node.start_point[0] + 1
        mod_node = node.child_by_field_name("module_name")
        if mod_node is None:
            return
        module = self._absolute(mod_node) if mod_node.type == "relative_import" else _text(mod_node)
        for child in node.named_children:
            if child == mod_node:
                continue
            if child.type == "dotted_name":
                name = _text(child)
                self.result.imports.append(Import(module=module, name=name, alias=name, line=line))
            elif child.type == "aliased_import":
                name = _text(child.child_by_field_name("name"))
                alias = _text(child.child_by_field_name("alias"))
                self.result.imports.append(Import(module=module, name=name, alias=alias, line=line))
            elif child.type == "wildcard_import":
                self.result.imports.append(Import(module=module, name="*", alias="*", line=line))
