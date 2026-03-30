#!/usr/bin/env python3
"""Test all HTTP MCP servers together."""

import asyncio
from src.mcp_client import MultiMCPClient


async def main():
    print("=" * 80)
    print("Test: All HTTP MCP Servers")
    print("=" * 80)

    client = MultiMCPClient()

    try:
        print("\n1. Connecting to all servers...")
        await client.connect()
        print(f"   ✓ Connected to {len(client.sessions)} servers:")
        for name in client.sessions.keys():
            print(f"     - {name}")

        print("\n2. Listing all tools...")
        tools = await client.list_tools()
        print(f"   ✓ Found {len(tools)} tools total:")
        for tool in tools:
            print(f"     - {tool['name']} (from {tool['_server']})")

        print("\n3. Testing promptfoo tool...")
        result = await client.call_tool("list_evaluations", {})
        print(f"   ✓ list_evaluations works")

        print("\n4. Testing synthetic-eval tool...")
        result = await client.call_tool(
            "generate_questions",
            {"prompt": "A todo app", "numSamples": 1, "numPersonas": 1},
        )
        print(f"   ✓ generate_questions works")

        print("\n5. Testing viewer tool...")
        result = await client.call_tool("get_viewer_url", {})
        print(f"   ✓ get_viewer_url returned: {result.content[0].text}")

        print("\n✓ All tests passed! HTTP transport is working perfectly.")

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
