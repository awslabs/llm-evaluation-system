#!/usr/bin/env python3
"""Test HTTP MCP server."""

import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    print("=" * 80)
    print("Test: HTTP MCP Server")
    print("=" * 80)

    # Connect to HTTP server
    async with streamablehttp_client("http://localhost:8002/mcp") as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            # Initialize
            print("\n1. Initializing...")
            await session.initialize()
            print("   ✓ Connected")

            # List tools
            print("\n2. Listing tools...")
            tools = await session.list_tools()
            print(f"   Available tools: {[tool.name for tool in tools.tools]}")

            # Call generate_questions
            print("\n3. Calling generate_questions...")
            result = await session.call_tool(
                "generate_questions",
                {"prompt": "A weather forecasting app", "numSamples": 2, "numPersonas": 2},
            )
            print(f"   ✓ Result: {result.content[0].text[:200]}...")

            print("\n✓ All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
