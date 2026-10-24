"""Run the same tool-using agent with and without ContextGraph and compare.

The agent talks to any OpenAI-compatible chat completions endpoint:

    export CG_LLM_API_KEY=...                       # or put KEY=value lines in eval/.env
    export CG_LLM_BASE_URL=https://openrouter.ai/api/v1   # default
    export CG_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b:free  # default
    python eval/run_eval.py                 # all questions, both modes
    python eval/run_eval.py --only req-01   # one question

Baseline tools: list_files, read_file, grep. ContextGraph mode adds the eight graph tools
(called in-process; they return exactly what the MCP server returns).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from contextgraph import tools as cg_tools
from contextgraph.indexer import index_repo

HERE = Path(__file__).resolve().parent
REPOS = HERE / "repos"
RESULTS = HERE / "results"
MAX_TURNS = 20
READ_LIMIT = 300  # lines per read_file call
GREP_LIMIT = 60  # matches per grep call

SYSTEM = (
    "You answer questions about the Python repository in the current directory. "
    "Use the tools to look at the code; do not guess. When you are confident, reply with a short "
    "final answer that names the exact functions, methods, classes, constants or files asked for."
)

BASE_TOOLS = [
    {"name": "list_files", "description": "List files under a directory (recursive, .py files only).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}},
    {"name": "read_file", "description": f"Read lines of a file (at most {READ_LIMIT} lines per call).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer", "default": 1},
         "end_line": {"type": "integer"}}, "required": ["path"]}},
    {"name": "grep", "description": f"Regex search in .py files, returns up to {GREP_LIMIT} `path:line: text` matches.",
     "parameters": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["pattern"]}},
]

_sym = {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}
_sym_depth = {"type": "object", "properties": {"symbol": {"type": "string"}, "depth": {"type": "integer", "default": 1}},
              "required": ["symbol"]}
_file = {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]}
GRAPH_TOOLS = [
    {"name": "search_symbol", "description": "Find classes/functions/methods by name.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "kind": {"type": "string"}},
                    "required": ["query"]}},
    {"name": "get_definition", "description": "Source of one symbol (qualified or unique short name).", "parameters": _sym},
    {"name": "find_callers", "description": "Functions/methods that call a symbol, with call sites. depth>1 is transitive.",
     "parameters": _sym_depth},
    {"name": "find_callees", "description": "What a symbol calls (repo functions and external calls).", "parameters": _sym_depth},
    {"name": "get_dependencies", "description": "Modules a file imports.", "parameters": _file},
    {"name": "get_dependents", "description": "Files that import a file.", "parameters": _file},
    {"name": "get_context", "description": "A symbol plus its callers and callees, trimmed to a token budget.",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "depth": {"type": "integer", "default": 1},
                    "max_tokens": {"type": "integer", "default": 3000}}, "required": ["symbol"]}},
    {"name": "repo_overview", "description": "Module tree, one line per file.", "parameters": {"type": "object", "properties": {}}},
]
GRAPH_INSTRUCTIONS = (
    " You also have ContextGraph tools that index the code as a graph of symbols, calls, imports and "
    "inheritance. Prefer them over grep and reading whole files."
)


@dataclass
class Run:
    id: str
    repo: str
    mode: str
    answer: str = ""
    score: float = 0.0
    correct: bool = False
    missing: list = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    files_read: list = field(default_factory=list)
    tool_calls: int = 0
    turns: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Workspace:
    def __init__(self, root: Path, graph=None) -> None:
        self.root = root
        self.cg = graph
        self.files_read: set[str] = set()

    def _safe(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if self.root not in (p, *p.parents):
            raise ValueError("path outside the repo")
        return p

    def call(self, name: str, args: dict) -> str:
        try:
            return self._call(name, args)
        except cg_tools.ToolError as e:
            return f"error: {e}"
        except Exception as e:  # report tool errors to the model instead of crashing the run
            return f"error: {type(e).__name__}: {e}"

    def _call(self, name: str, a: dict) -> str:
        if name == "list_files":
            base = self._safe(a.get("path") or ".")
            files = sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*.py") if ".git" not in p.parts)
            return "\n".join(files[:400]) + (f"\n... {len(files) - 400} more" if len(files) > 400 else "")
        if name == "read_file":
            path = a["path"]
            lines = self._safe(path).read_text(errors="replace").splitlines()
            start = max(int(a.get("start_line") or 1), 1)
            end = min(int(a.get("end_line") or start + READ_LIMIT - 1), start + READ_LIMIT - 1, len(lines))
            self.files_read.add(self._safe(path).relative_to(self.root).as_posix())
            body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
            more = f"\n[file has {len(lines)} lines; showing {start}-{end}]" if end < len(lines) or start > 1 else ""
            return body + more
        if name == "grep":
            base = self._safe(a.get("path") or ".")
            out = subprocess.run(["grep", "-rnE", "--include=*.py", a["pattern"], str(base)],
                                 capture_output=True, text=True).stdout.splitlines()
            out = [o.replace(str(self.root) + "/", "", 1)[:300] for o in out]
            return "\n".join(out[:GREP_LIMIT]) + (f"\n... {len(out) - GREP_LIMIT} more" if len(out) > GREP_LIMIT else "") or "no matches"
        if self.cg is None:
            return f"error: unknown tool {name}"
        cg = self.cg
        depth = int(a.get("depth") or 1)
        if name == "search_symbol":
            return cg_tools.search_symbol(cg, a["query"], a.get("kind"))
        if name == "get_definition":
            return cg_tools.get_definition(cg, a["symbol"])
        if name == "find_callers":
            return cg_tools.find_callers(cg, a["symbol"], depth)
        if name == "find_callees":
            return cg_tools.find_callees(cg, a["symbol"], depth)
        if name == "get_dependencies":
            return cg_tools.get_dependencies(cg, a["file"])
        if name == "get_dependents":
            return cg_tools.get_dependents(cg, a["file"], depth)
        if name == "get_context":
            return cg_tools.get_context(cg, a["symbol"], depth, int(a.get("max_tokens") or 3000))
        if name == "repo_overview":
            return cg_tools.repo_overview(cg)
        return f"error: unknown tool {name}"


def load_env(path: Path = HERE / ".env") -> None:
    """KEY=value lines from eval/.env (gitignored), without overriding the real environment."""
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class LLM:
    def __init__(self) -> None:
        load_env()
        os.environ.setdefault("CG_LLM_BASE_URL", "https://openrouter.ai/api/v1")
        os.environ.setdefault("CG_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
        try:
            self.base = os.environ["CG_LLM_BASE_URL"].rstrip("/")
            self.key = os.environ["CG_LLM_API_KEY"]
            self.model = os.environ["CG_LLM_MODEL"]
        except KeyError as e:
            sys.exit(f"set {e.args[0]} (see the docstring at the top of this file)")
        self.http = httpx.Client(timeout=120)

    def chat(self, messages: list, tools: list) -> dict:
        body = {"model": self.model, "messages": messages, "max_tokens": 2000, "temperature": 0,
                "tools": [{"type": "function", "function": t} for t in tools]}
        for attempt in range(6):
            r = self.http.post(f"{self.base}/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {self.key}"})
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"HTTP {r.status_code} after retries: {r.text[:300]}")


def grade(answer: str, expect: list) -> tuple[float, list]:
    missing = []
    for item in expect:
        options = item if isinstance(item, list) else [item]
        if not any(re.search(rf"(?<!\w){re.escape(o)}(?!\w)", answer, re.IGNORECASE) for o in options):
            missing.append(options[0])
    return 1 - len(missing) / len(expect), missing


def run_one(llm: LLM, q: dict, mode: str, graphs: dict) -> Run:
    root = REPOS / q["repo"]
    ws = Workspace(root, graphs[q["repo"]] if mode == "contextgraph" else None)
    tools = BASE_TOOLS + (GRAPH_TOOLS if mode == "contextgraph" else [])
    system = SYSTEM + (GRAPH_INSTRUCTIONS if mode == "contextgraph" else "")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": q["question"]}]
    run = Run(id=q["id"], repo=q["repo"], mode=mode)
    started = time.perf_counter()
    try:
        for turn in range(MAX_TURNS):
            run.turns = turn + 1
            resp = llm.chat(messages, tools)
            usage = resp.get("usage") or {}
            run.prompt_tokens += usage.get("prompt_tokens", 0)
            run.completion_tokens += usage.get("completion_tokens", 0)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": calls} if calls else {})})
            if not calls:
                run.answer = msg.get("content") or ""
                break
            for c in calls:
                run.tool_calls += 1
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                out = ws.call(c["function"]["name"], args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out[:20000]})
        else:
            run.error = "turn limit"
    except Exception as e:
        run.error = f"{type(e).__name__}: {e}"
    run.seconds = round(time.perf_counter() - started, 1)
    run.files_read = sorted(ws.files_read)
    run.score, run.missing = grade(run.answer, q["expect"])
    run.correct = not run.missing
    return run


def ensure_repos(spec: dict) -> None:
    REPOS.mkdir(exist_ok=True)
    for name, r in spec.items():
        if not (REPOS / name).exists():
            subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", r["ref"], r["url"], str(REPOS / name)],
                           check=True)


def summarize(runs: list[Run]) -> str:
    def agg(rs: list[Run]) -> dict:
        n = len(rs) or 1
        return {
            "accuracy": sum(r.correct for r in rs) / n,
            "recall": sum(r.score for r in rs) / n,
            "tokens": sum(r.tokens for r in rs) / n,
            "files": sum(len(r.files_read) for r in rs) / n,
            "calls": sum(r.tool_calls for r in rs) / n,
            "seconds": sum(r.seconds for r in rs) / n,
        }

    rows = ["| Repo | Mode | Fully correct | Answer recall | Avg tokens | Avg files read | Avg tool calls | Avg time (s) |",
            "|---|---|---|---|---|---|---|---|"]
    for repo in [*sorted({r.repo for r in runs}), "all"]:
        for mode in ("baseline", "contextgraph"):
            rs = [r for r in runs if r.mode == mode and (repo == "all" or r.repo == repo)]
            if not rs:
                continue
            a = agg(rs)
            name = f"**{repo}**" if repo == "all" else repo
            rows.append(f"| {name} | {mode} | {sum(r.correct for r in rs)}/{len(rs)} ({a['accuracy']:.0%}) | "
                        f"{a['recall']:.0%} | {a['tokens']:,.0f} | {a['files']:.1f} | {a['calls']:.1f} | {a['seconds']:.1f} |")
    return "\n".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="question ids")
    ap.add_argument("--modes", nargs="*", default=["baseline", "contextgraph"])
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    spec = json.loads((HERE / "questions.json").read_text())
    ensure_repos(spec["repos"])
    questions = [q for q in spec["questions"] if not args.only or q["id"] in args.only]
    graphs = {}
    for name in {q["repo"] for q in questions}:
        graphs[name], stats = index_repo(REPOS / name)
        print(f"indexed {name}: {stats.files} files in {stats.seconds}s")
    llm = LLM()
    jobs = [(q, m) for q in questions for m in args.modes]
    with ThreadPoolExecutor(args.workers) as pool:
        runs = list(pool.map(lambda qm: run_one(llm, *qm, graphs), jobs))
    for r in runs:
        flag = "ok " if r.correct else "MISS"
        print(f"{flag} {r.id:9} {r.mode:12} tokens={r.tokens:>7,} files={len(r.files_read):>2} "
              f"t={r.seconds:>5}s {('missing ' + ', '.join(r.missing)) if r.missing else ''} {r.error}")
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "results.json").write_text(json.dumps(
        {"model_env": "CG_LLM_MODEL", "max_turns": MAX_TURNS, "runs": [asdict(r) | {"tokens": r.tokens} for r in runs]},
        indent=1))
    table = summarize(runs)
    (RESULTS / "summary.md").write_text(table + "\n")
    print("\n" + table)


if __name__ == "__main__":
    main()
