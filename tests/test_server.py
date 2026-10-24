import anyio
from mcp import Client

from contextgraph.server import create_server


def test_tools_over_mcp(repo):
    async def go():
        async with Client(create_server(repo)) as client:
            listed = {t.name for t in (await client.list_tools()).tools}
            assert {"search_symbol", "get_definition", "find_callers", "find_callees", "get_dependencies",
                    "get_dependents", "get_context", "repo_overview"} <= listed
            res = await client.call_tool("find_callers", {"symbol": "Catalog.find"})
            text = res.content[0].text
            assert "bookshop.orders.quote" in text
            res = await client.call_tool("get_definition", {"symbol": "nope_nothing"})
            assert res.content[0].text.startswith("error:")

    anyio.run(go)
