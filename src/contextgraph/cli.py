"""Command line: `contextgraph index <repo>`, `contextgraph serve [repo]`, `contextgraph query ...`."""

from __future__ import annotations

import argparse
import sys

from contextgraph import __version__, tools
from contextgraph.indexer import index_repo, load


def _index(args: argparse.Namespace) -> int:
    cg, stats = index_repo(args.repo, args.out, force=args.force)
    s = cg.stats()
    print(f"indexed {stats.files} files in {stats.seconds}s "
          f"({stats.parsed} parsed, {stats.reused} unchanged, {stats.removed} removed)")
    print("nodes: " + ", ".join(f"{k}={v}" for k, v in sorted(s["nodes"].items())))
    print("edges: " + ", ".join(f"{k}={v}" for k, v in sorted(s["edges"].items())))
    if stats.failed:
        print("failed to parse: " + ", ".join(stats.failed), file=sys.stderr)
    return 0


def _serve(args: argparse.Namespace) -> int:
    from contextgraph.server import serve

    serve(args.repo, args.out, transport=args.transport, host=args.host, port=args.port)
    return 0


QUERIES = {
    "search_symbol": (tools.search_symbol, ["query"]),
    "get_definition": (tools.get_definition, ["symbol"]),
    "find_callers": (tools.find_callers, ["symbol"]),
    "find_callees": (tools.find_callees, ["symbol"]),
    "get_dependencies": (tools.get_dependencies, ["file"]),
    "get_dependents": (tools.get_dependents, ["file"]),
    "get_context": (tools.get_context, ["symbol"]),
    "repo_overview": (tools.repo_overview, []),
}


def _query(args: argparse.Namespace) -> int:
    fn, params = QUERIES[args.tool]
    if len(args.args) != len(params):
        print(f"usage: contextgraph query {args.tool} {' '.join('<' + p + '>' for p in params)}", file=sys.stderr)
        return 2
    try:
        cg = load(args.repo, args.out)
    except FileNotFoundError:
        cg, _ = index_repo(args.repo, args.out)
    kwargs = {}
    if args.tool in ("find_callers", "find_callees", "get_dependents", "get_context"):
        kwargs["depth"] = args.depth
    if args.tool == "get_context":
        kwargs["max_tokens"] = args.max_tokens
    try:
        print(fn(cg, *args.args, **kwargs))
    except tools.ToolError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="contextgraph", description="Queryable code graph for coding agents.")
    p.add_argument("--version", action="version", version=f"contextgraph {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("index", help="parse a repo and write the graph to <repo>/.contextgraph")
    pi.add_argument("repo")
    pi.add_argument("--out", help="index directory (default: <repo>/.contextgraph)")
    pi.add_argument("--force", action="store_true", help="re-parse every file")
    pi.set_defaults(func=_index)

    ps = sub.add_parser("serve", help="run the MCP server")
    ps.add_argument("repo", nargs="?", default=".")
    ps.add_argument("--out", help="index directory (default: <repo>/.contextgraph)")
    ps.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)
    ps.set_defaults(func=_serve)

    pq = sub.add_parser("query", help="run one tool from the shell, e.g. `query find_callers Session.send`")
    pq.add_argument("tool", choices=sorted(QUERIES))
    pq.add_argument("args", nargs="*")
    pq.add_argument("--repo", default=".")
    pq.add_argument("--out")
    pq.add_argument("--depth", type=int, default=1)
    pq.add_argument("--max-tokens", type=int, default=4000)
    pq.set_defaults(func=_query)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
