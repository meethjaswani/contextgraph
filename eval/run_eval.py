"""Run the same tool-using agent with and without ContextGraph and compare.

The agent talks to any OpenAI-compatible chat completions endpoint:

    export CG_LLM_API_KEY=...                       # or put KEY=value lines in eval/.env
    export CG_LLM_BASE_URL=https://openrouter.ai/api/v1   # default
    export CG_LLM_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free  # default
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
# Budgets apply to both modes equally. They are sized so every request fits a small
# per-minute token limit (8K on free tiers): tool output is capped, and once the
# conversation grows past HISTORY_TOKENS the oldest tool outputs are replaced by a stub.
MAX_TURNS = 12
READ_LIMIT = 80  # lines per read_file call
GREP_LIMIT = 25  # matches per grep call
TOOL_CHARS = 4000  # max characters of one tool result
HISTORY_TOKENS = 4500  # estimated tokens of conversation sent per request
CONTEXT_TOKENS = 900  # get_context budget

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
                    "max_tokens": {"type": "integer", "default": 900}}, "required": ["symbol"]}},
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
    trace: list = field(default_factory=list)  # tool name + args, in order
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
            return "\n".join(files[:150]) + (f"\n... {len(files) - 150} more" if len(files) > 150 else "")
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
            return cg_tools.get_context(cg, a["symbol"], depth, min(int(a.get("max_tokens") or CONTEXT_TOKENS), CONTEXT_TOKENS))
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
        os.environ.setdefault("CG_LLM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
        try:
            self.base = os.environ["CG_LLM_BASE_URL"].rstrip("/")
            self.key = os.environ["CG_LLM_API_KEY"]
            self.model = os.environ["CG_LLM_MODEL"]
        except KeyError as e:
            sys.exit(f"set {e.args[0]} (see the docstring at the top of this file)")
        self.http = httpx.Client(timeout=120)

    def chat(self, messages: list, tools: list) -> dict:
        body = {"model": self.model, "messages": messages, "max_tokens": 1500, "temperature": 0,
                "tools": [{"type": "function", "function": t} for t in tools]}
        if os.environ.get("CG_LLM_REASONING_EFFORT"):
            body["reasoning_effort"] = os.environ["CG_LLM_REASONING_EFFORT"]
        bad_output = 0
        for attempt in range(12):
            r = self.http.post(f"{self.base}/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {self.key}"})
            if r.status_code == 429 and re.search(r"per-day|per day|\(TPD\)|\(RPD\)", r.text):
                raise RuntimeError(f"daily quota reached: {r.text[:200]}")
            if r.status_code == 429:
                wait = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", r.text)
                time.sleep((int(wait.group(1) or 0) * 60 + float(wait.group(2)) + 1) if wait else 2 ** min(attempt, 5) * 2)
                continue
            if r.status_code in (500, 502, 503, 529):
                time.sleep(2 ** min(attempt, 5) * 2)
                continue
            if r.status_code == 400 and re.search(r"output_parse_failed|tool_use_failed", r.text) and bad_output < 3:
                # The model emitted a malformed tool call; sample again.
                bad_output += 1
                body["temperature"] = 0.3
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"HTTP {r.status_code} after retries: {r.text[:300]}")


def _tokens(messages: list) -> int:
    return sum(len(json.dumps(m)) for m in messages) // 4


def trim_history(messages: list) -> None:
    """Stub out the oldest tool results until the conversation fits HISTORY_TOKENS (latest result kept)."""
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    for i in tool_idx[:-1]:
        if _tokens(messages) <= HISTORY_TOKENS:
            return
        if not messages[i]["content"].startswith("[earlier output removed"):
            messages[i]["content"] = f"[earlier output removed to save space: {len(messages[i]['content'])} chars]"


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
            trim_history(messages)
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
                run.trace.append({"tool": c["function"]["name"], "args": args})
                out = ws.call(c["function"]["name"], args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out[:TOOL_CHARS] + ("\n[truncated]" if len(out) > TOOL_CHARS else "")})
        else:
            run.answer = "(no final answer within the turn limit)"
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
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--resume", action="store_true", help="keep finished runs from results.json, redo the rest")
    args = ap.parse_args()
    spec = json.loads((HERE / "questions.json").read_text())
    ensure_repos(spec["repos"])
    questions = [q for q in spec["questions"] if not args.only or q["id"] in args.only]
    graphs = {}
    for name in {q["repo"] for q in questions}:
        graphs[name], stats = index_repo(REPOS / name)
        print(f"indexed {name}: {stats.files} files in {stats.seconds}s")
    llm = LLM()
    out = RESULTS / "results.json"
    done: dict[tuple[str, str], Run] = {}
    if args.resume and out.exists():
        for d in json.loads(out.read_text())["runs"]:
            d.pop("tokens", None)
            run = Run(**d)
            if not run.error:  # keep finished runs, redo errored ones
                done[(run.id, run.mode)] = run
    jobs = [(q, m) for q in questions for m in args.modes if (q["id"], m) not in done]
    print(f"{len(done)} runs kept, {len(jobs)} to run with {llm.model}")
    with ThreadPoolExecutor(args.workers) as pool:
        fresh = list(pool.map(lambda qm: run_one(llm, *qm, graphs), jobs))
    for r in fresh:
        flag = "ok " if r.correct else "MISS"
        print(f"{flag} {r.id:9} {r.mode:12} tokens={r.tokens:>7,} files={len(r.files_read):>2} "
              f"t={r.seconds:>5}s {('missing ' + ', '.join(r.missing)) if r.missing else ''} {r.error[:120]}")
    order = {(q["id"], m): i for i, (q, m) in enumerate((q, m) for q in spec["questions"] for m in ("baseline", "contextgraph"))}
    runs = sorted([*done.values(), *fresh], key=lambda r: order.get((r.id, r.mode), 0))
    RESULTS.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {"model": llm.model, "max_turns": MAX_TURNS, "runs": [asdict(r) | {"tokens": r.tokens} for r in runs]},
        indent=1))
    errored = [r for r in runs if r.error]
    if errored:
        print(f"\n{len(errored)} runs errored; rerun with --resume to retry only those")
    # Compare like with like: only questions finished in every mode.
    finished = {}
    for r in runs:
        if not r.error:
            finished.setdefault(r.id, set()).add(r.mode)
    runs = [r for r in runs if not r.error and finished[r.id] >= set(args.modes)]
    table = summarize(runs)
    (RESULTS / "summary.md").write_text(table + "\n")
    print("\n" + table)


if __name__ == "__main__":
    main()
