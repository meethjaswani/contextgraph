# ContextGraph

An MCP server that turns a codebase into a queryable graph, so AI coding agents fetch only the code they need instead of reading the whole repo.

![contextgraph answering graph queries on the sample repo](docs/images/demo.gif)

## Why

On a large repo a coding agent either reads too much (whole files, wasted tokens) or too little (grep hits with no callers or dependencies). ContextGraph gives it a map of the code: what is defined where, what calls what, who imports whom. The agent asks a precise question and gets back a few hundred tokens instead of a few files.

## How it works

![Architecture](docs/architecture.svg)

1. **Parse.** tree-sitter parses every source file. The parser layer is a small registry keyed by file extension (`contextgraph/parsers/`); Python ships today, and JavaScript/TypeScript can be added by implementing `LanguageParser`.
2. **Extract.** Each file yields
   - nodes: files, classes, functions and methods, with path, line range, signature and docstring
   - edges: `defines`, `imports`, `calls`, `inherits`
3. **Build the graph.** Everything goes into a NetworkX `MultiDiGraph`. Calls and imports are resolved to their definitions inside the repo through local scopes, imports, package re-exports (`from .sessions import Session` in `__init__.py`), `self`/`cls`/`super()`, annotated parameters (`catalog: Catalog` → `catalog.find()`), simple assignments (`x = Foo()`) and return annotations (`get_gateway(...).charge()`). Calls into other packages stay as external leaf nodes (`ext:os.path.join`). When the receiver's type is unknown, a call is linked only if the method name is unique in the repo, and the edge is marked `matched by name`.
4. **Index incrementally.** The graph and a per-file parse cache keyed by SHA-256 are saved to `<repo>/.contextgraph/`. Re-indexing only re-parses files whose hash changed (requests: 0.08 s cold, 0.03 s warm). The server checks for changed files every few seconds and refreshes on its own.
5. **Serve over MCP.**

| Tool | What it returns |
|---|---|
| `search_symbol(query, kind?)` | classes/functions/methods by name, best match first |
| `get_definition(symbol)` | the source of one symbol with file and line range |
| `find_callers(symbol, depth=1)` | who calls it, with call-site lines; `depth>1` follows callers of callers |
| `find_callees(symbol, depth=1)` | what it calls, in the repo and external |
| `get_dependencies(file)` | modules the file imports, split into in-repo and external |
| `get_dependents(file, depth=1)` | files that import it, optionally transitively |
| `get_context(symbol, depth=1, max_tokens=4000)` | the symbol plus callers and callees up to N hops, trimmed to a token budget |
| `repo_overview()` | module tree with one line per file |
| `reindex()` | force a refresh (normally automatic) |

Symbols can be passed as qualified names (`requests.sessions.Session.send`), dotted suffixes (`Session.send`) or bare names when unique. Ambiguous names return the candidates.

`get_context` puts the target's full source first, then neighbours by hop distance. A neighbour gets its full source if it fits the budget, otherwise its outline (signature, docstring, member signatures), otherwise it is listed under "omitted". Tokens are estimated at four characters per token.

![get_context on the sample repo](docs/images/context.png)

On a real repo (requests): a cold index takes 0.08 s, a re-index with no changes 0.03 s.

![contextgraph indexing requests and answering graph queries](docs/images/index.png)

## Quickstart

```bash
git clone https://github.com/meethjaswani/contextgraph && cd contextgraph
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

contextgraph index examples/bookshop        # parse + build the graph
contextgraph query find_callers total --repo examples/bookshop
contextgraph serve examples/bookshop        # MCP over stdio
```

`contextgraph serve --transport http --port 8000` serves streamable HTTP at `http://127.0.0.1:8000/mcp` instead.

Add `.contextgraph/` to your repo's `.gitignore`.

## Connect it to an MCP client

Most MCP clients take a JSON block like this (the file name depends on the client, e.g. `.mcp.json`, `.cursor/mcp.json` or `.vscode/mcp.json`):

```json
{
  "mcpServers": {
    "contextgraph": {
      "command": "/path/to/contextgraph/.venv/bin/contextgraph",
      "args": ["serve", "/path/to/your/repo"]
    }
  }
}
```

The server sends instructions on connect telling the agent to start with `repo_overview` or `search_symbol` and to use `get_context` before reading whole files.

## Example tool calls

On the bundled sample repo (`examples/bookshop`, already indexed in `examples/bookshop/.contextgraph/`):

![repo_overview and find_callees on the sample repo](docs/images/overview.png)

```text
> find_callers(symbol="with_tax", depth=2)
callers of bookshop.pricing.with_tax:
  bookshop.pricing.total  defined bookshop/pricing.py:23; call sites: bookshop/pricing.py:25
  [hop 2] bookshop.orders.place_order  defined bookshop/orders.py:19; call sites: bookshop/orders.py:23
  [hop 2] bookshop.orders.quote  defined bookshop/orders.py:29; call sites: bookshop/orders.py:32

> find_callers(symbol="Catalog.find")
callers of bookshop.catalog.Catalog.find:
  bookshop.catalog.Catalog.reserve  defined bookshop/catalog.py:27; call sites: bookshop/catalog.py:29
  bookshop.orders.quote  defined bookshop/orders.py:29; call sites: bookshop/orders.py:31

> get_dependents(file="bookshop/config.py", depth=2)
files that import bookshop/config.py:
  bookshop/notify.py
  bookshop/pricing.py
  [hop 2] bookshop/orders.py

> get_definition(symbol="_send")
error: '_send' is ambiguous, pass one of these qualified names:
  bookshop.payments.gateway.CardGateway._send  (method, bookshop/payments/gateway.py:24)
  bookshop.payments.gateway.FakeGateway._send  (method, bookshop/payments/gateway.py:33)
  bookshop.payments.gateway.Gateway._send  (method, bookshop/payments/gateway.py:19)
```

`quote` is found as a caller of `Catalog.find` because its `catalog` parameter is annotated as `Catalog`; `place_order → Gateway.charge` is found through the return annotation of `get_gateway`.

## Evaluation

<!-- EVAL:START -->
15 questions (5 per repo), each run once without and once with ContextGraph, on free-tier models from Groq.

| Repo | Mode | Fully correct | Answer recall | Avg tokens | Avg files read | Avg tool calls | Avg time (s) |
|---|---|---|---|---|---|---|---|
| **all** | baseline | 8/15 (53%) | 65% | 8,710 | 1.2 | 4.9 | 51.3 |
| **all** | contextgraph | 11/15 (73%) | 79% | 8,338 | 0.6 | 4.0 | 64.6 |

With ContextGraph the agent answered **11 of 15** questions fully (baseline: 8), named **79%** of the expected items (baseline: 65%), and opened **half as many files** (0.6 vs 1.2 per question) with fewer tool calls (4.0 vs 4.9). Average tokens were about the same (8.3K vs 8.7K).

<details>
<summary>By repo and by question</summary>

| Repo | Mode | Fully correct | Answer recall | Avg tokens | Avg files read | Avg tool calls | Avg time (s) |
|---|---|---|---|---|---|---|---|
| click | baseline | 2/5 (40%) | 66% | 10,840 | 1.2 | 5.4 | 60.8 |
| click | contextgraph | 2/5 (40%) | 56% | 11,478 | 0.8 | 5.6 | 68.1 |
| requests | baseline | 1/5 (20%) | 30% | 11,596 | 1.6 | 7.0 | 70.1 |
| requests | contextgraph | 4/5 (80%) | 80% | 9,179 | 0.8 | 4.6 | 55.9 |
| rich | baseline | 5/5 (100%) | 100% | 3,694 | 0.8 | 2.4 | 23.0 |
| rich | contextgraph | 5/5 (100%) | 100% | 4,357 | 0.2 | 1.8 | 70.0 |

| Question | Model | Baseline | ContextGraph | Baseline tokens | ContextGraph tokens |
|---|---|---|---|---|---|
| req-01 | openai/gpt-oss-20b | missed merge_environment_settings | missed merge_environment_settings, resolve_proxies | 3,607 | 27,204 |
| req-02 | openai/gpt-oss-20b | correct | correct | 11,600 | 12,064 |
| req-04 | openai/gpt-oss-20b | missed resolve_redirects | correct | 19,408 | 2,013 |
| req-05 | openai/gpt-oss-20b | missed Session.send | correct | 4,556 | 2,167 |
| req-07 | openai/gpt-oss-20b | missed prepare_body, prepare_content_length | correct | 18,807 | 2,449 |
| click-01 | openai/gpt-oss-20b | correct | correct | 1,814 | 11,030 |
| click-02 | openai/gpt-oss-20b | missed resolve_command, _parse_decls, get_help_record, join_options, normalize_opt, __init__ | missed resolve_command, _parse_decls, get_help_record, join_options, normalize_opt, __init__ | 27,507 | 24,126 |
| click-05 | openai/gpt-oss-20b | missed Context | missed auto_envvar_prefix, Context | 4,828 | 16,931 |
| click-07 | qwen/qwen3.8-27b | correct | correct | 2,423 | 2,456 |
| click-08 | qwen/qwen3.8-27b | missed _process_result | missed forward | 17,628 | 2,846 |
| rich-01 | qwen/qwen3.8-27b | correct | correct | 2,280 | 3,262 |
| rich-03 | qwen/qwen3.8-27b | correct | correct | 3,007 | 2,548 |
| rich-04 | qwen/qwen3.8-27b | correct | correct | 3,451 | 9,839 |
| rich-06 | qwen/qwen3.8-27b | correct | correct | 8,041 | 3,517 |
| rich-10 | qwen/qwen3.8-27b | correct | correct | 1,690 | 2,617 |

</details>

**What to read into this, and what not to:**

- **Where the graph helps most is "who calls X".** In requests, the baseline agent grepped for the call, opened the file at the matching line and named the wrong enclosing function or stopped short (`req-04`: `send` instead of `resolve_redirects`; `req-05` and `req-07` likewise). `find_callers` returns the enclosing function directly, so those three ContextGraph runs were correct with 2.0–2.4K tokens each, against 4.6–19K for the baseline.
- **The graph does not help with config lookups.** "Where is X configured" questions (`req-02`, `click-05`, `rich-04`) are a grep job, and `rich-04` cost more tokens with ContextGraph because the agent explored with graph tools first.
- **Small free models do not always use the tools they are given.** In `req-01` the ContextGraph agent never called a graph tool and ran out of turns re-reading the same file; `find_callers get_environ_proxies` returns both expected callers.
- **Rich was too easy to separate the two modes:** both got 5/5.
- **Time is not comparable.** Most of the wall-clock time is the harness waiting out the free tier's per-minute limit, so the time column mostly measures queueing.
- **Small sample, two models.** Free-tier daily token limits (200K per model) did not allow all 30 questions on one model. Questions `req-*`, `click-01`, `click-02` and `click-05` ran on `openai/gpt-oss-20b`; the rest on `qwen/qwen3.8-27b`. Both modes of a question always ran on the same model. The full 30 questions run with `python eval/run_eval.py --all`.
- **One harness bug during this run:** `read_file` ignored the `line_start`/`line_end` argument names some models use, so those calls read from line 1. It affected both modes and is fixed now; the numbers above are from before the fix.

Raw runs, including every tool call and answer: [`eval/results/results.json`](eval/results/results.json).
<!-- EVAL:END -->

### Method

- **Repos:** [requests](https://github.com/psf/requests) v2.32.3 (36 Python files), [click](https://github.com/pallets/click) 8.1.7 (71) and [rich](https://github.com/Textualize/rich) v13.7.1 (190).
- **Questions:** 30 in [`eval/questions.json`](eval/questions.json), ten per repo, of which 15 (the `core` list) were run for the table above: who calls X, what does X inherit from, where is Y configured, what breaks if I change Z, which files import W. Every expected answer was checked by hand against the source with grep, not taken from the tool.
- **Agent:** the same tool-calling loop, model, prompt and budgets in both modes, up to 12 turns. Baseline tools are `list_files`, `read_file` (80 lines per call) and `grep` (25 matches). The ContextGraph mode gets the same three tools plus the eight graph tools, called in-process so they return exactly what the MCP server returns (`get_context` capped at 900 tokens). The budgets keep every request under 8K tokens so the eval runs on free API tiers: tool results are capped at 4,000 characters, and once a conversation passes about 4,500 tokens the oldest tool results are replaced by a short stub.
- **Grading:** an answer must name every expected item (case-insensitive, whole-word match). "Fully correct" counts answers that name all of them; "answer recall" is the share of expected items named. Tokens are prompt plus completion tokens summed over every model call in a run, as reported by the API. "Files read" counts distinct files opened with `read_file`.
- **Run it yourself:** put `CG_LLM_API_KEY`, `CG_LLM_BASE_URL` and `CG_LLM_MODEL` in `eval/.env` (any OpenAI-compatible endpoint, e.g. Groq or OpenRouter), then `pip install -e ".[eval]"` and `python eval/run_eval.py`. It paces itself to the provider's rate limits; `--resume` keeps finished runs and retries the rest. Per-run results, including every tool call, land in `eval/results/`.

## Tests

```bash
pytest
```

30 tests cover the parser (symbols, signatures, docstrings, relative imports, call expressions, type hints, broken files), graph queries (resolution through re-exports, `self`, typed parameters and return types, transitive callers, dependents, token budgets), incremental indexing (unchanged, changed and deleted files) and the MCP server end to end through an in-process client.

## Layout

```
src/contextgraph/
  parsers/        LanguageParser interface, registry, Python parser
  models.py       Symbol / Import / Call / FileResult records
  graph.py        graph builder, name resolution, queries
  indexer.py      file discovery, hashing, save/load
  tools.py        text output shared by the server and the CLI
  server.py       MCP server
  cli.py          contextgraph index | serve | query
examples/bookshop sample repo with its index committed
eval/             questions, harness, results
tests/
```

## Limits

- Python only for now.
- Calls through untyped variables, containers, `getattr` and dynamic dispatch are only linked when the method name is unique in the repo, and those edges are marked as name matches.
- Line numbers come from the last index; the server re-indexes when files change, but a query in the same second as an edit can see the old lines.

## License

MIT
