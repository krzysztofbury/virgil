"""Exercise the independently locked MCP script over real stdio, without credentials."""

import asyncio
from pathlib import Path

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def check() -> None:
    server = StdioServerParameters(
        command="uv",
        args=["run", "--locked", "--script", str(Path(__file__).resolve().parents[1] / "mcp_server/virgil_mcp.py")],
        env={"VIRGIL_API_URL": "", "VIRGIL_API_KEY": "", "CF_ACCESS_CLIENT_ID": "", "CF_ACCESS_CLIENT_SECRET": ""},
    )
    async with asyncio.timeout(60):
        async with stdio_client(server) as streams, ClientSession(*streams) as session:
            identity = await session.initialize()
            if identity.server_info.name != "virgil":
                raise RuntimeError("Unexpected MCP server identity")
            catalog = await session.list_tools()
            names = {tool.name for tool in catalog.tools}
            if not {"get_today_summary", "get_goals", "add_goal"} <= names:
                raise RuntimeError("MCP catalog is missing required capabilities")
            result = await session.call_tool("get_today_summary", {})
            if not result.is_error:
                raise RuntimeError("A tool call without credentials must fail")
            print(f"MCP stdio verified: {len(names)} tools, unauthenticated call refused.")


if __name__ == "__main__":
    asyncio.run(check())
