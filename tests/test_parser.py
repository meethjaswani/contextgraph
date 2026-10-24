from contextgraph.parsers import PythonParser, parser_for

SRC = b'''"""Module doc."""
import os.path
import json as j
from . import sibling
from ..pkg.mod import thing as other, Base
from .star import *


@decorator(1)
class Child(Base, metaclass=Meta):
    """A child."""

    def __init__(self, store: "Store"):
        self.store = store
        self.helper = make_helper()

    def run(self, n: int = 1) -> Result:
        x = Thing()
        self.step()
        super().run()
        x.go()
        return os.path.join("a", "b")

    @property
    def name(self):
        return "c"


def outer():
    def inner():
        return 1
    return inner()
'''


def parse():
    return PythonParser().parse("src/app/core/models.py", SRC)


def test_module_name_strips_src_and_init():
    p = PythonParser()
    assert p.module_name("src/app/core/models.py") == "app.core.models"
    assert p.module_name("app/__init__.py") == "app"
    assert p.module_name("setup.py") == "setup"


def test_registry_picks_parser_by_extension():
    assert isinstance(parser_for("a/b.py"), PythonParser)
    assert parser_for("a/b.rs") is None


def test_symbols_and_kinds():
    r = parse()
    kinds = {s.qualname: s.kind for s in r.symbols}
    assert kinds == {
        "app.core.models.Child": "class",
        "app.core.models.Child.__init__": "method",
        "app.core.models.Child.run": "method",
        "app.core.models.Child.name": "method",
        "app.core.models.outer": "function",
        "app.core.models.outer.inner": "function",
    }
    assert r.docstring == "Module doc."


def test_class_details():
    child = next(s for s in parse().symbols if s.name == "Child")
    assert child.bases == ["Base"]
    assert child.docstring == "A child."
    assert child.decorators == ["decorator(1)"]
    assert child.start_line == 9  # decorator line
    assert child.signature == "class Child(Base, metaclass=Meta)"
    assert child.locals["self.store"] == 'type:"Store"'
    assert child.locals["self.helper"] == "call:make_helper()"


def test_function_details():
    run = next(s for s in parse().symbols if s.name == "run")
    assert run.signature == "def run(self, n: int = 1) -> Result"
    assert run.returns == "Result"
    assert run.locals == {"n": "type:int", "x": "call:Thing()"}
    assert run.parent == "app.core.models.Child"


def test_imports_resolve_relative_modules():
    imps = [(i.module, i.name, i.alias) for i in parse().imports]
    assert ("os.path", None, "os.path") in imps
    assert ("json", None, "j") in imps
    assert ("app.core", "sibling", "sibling") in imps
    assert ("app.pkg.mod", "thing", "other") in imps
    assert ("app.pkg.mod", "Base", "Base") in imps
    assert ("app.core.star", "*", "*") in imps


def test_calls_record_caller_and_expression():
    calls = {(c.caller, c.callee) for c in parse().calls}
    run = "app.core.models.Child.run"
    assert (run, "Thing") in calls
    assert (run, "self.step") in calls
    assert (run, "super.run") in calls
    assert (run, "x.go") in calls
    assert (run, "os.path.join") in calls
    assert ("app.core.models.outer", "inner") in calls
    assert (None, "decorator") in calls  # decorators run at module level


def test_call_on_call_result():
    r = PythonParser().parse("m.py", b"def f():\n    get().save()\n    a.b().c()\n    (x or y).z()\n")
    callees = [c.callee for c in r.calls]
    assert "get().save" in callees
    assert "a.b().c" in callees
    assert "?.z" in callees


def test_syntax_errors_do_not_raise():
    r = PythonParser().parse("bad.py", b"def ok():\n    pass\n\ndef broken(:\n")
    assert any(s.name == "ok" for s in r.symbols)
