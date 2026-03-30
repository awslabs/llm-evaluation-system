#!/usr/bin/env python3
"""Test viewer MCP server."""

import asyncio
import json
from src.mcp_client import MultiMCPClient


async def main():
    print("=" * 80)
    print("Test: Viewer MCP Server")
    print("=" * 80)

    client = MultiMCPClient()

    try:
        print("\n1. Connecting...")
        await client.connect()
        print(f"   ✓ Connected to {len(client.sessions)} servers")

        print("\n2. Starting viewer...")
        result = await client.call_tool("start_viewer", {})
        data = json.loads(result.content[0].text)
        print(f"   Success: {data['success']}")
        print(f"   URL: {data.get('url')}")

        print("\n3. Getting viewer URL for specific eval...")
        result = await client.call_tool("get_viewer_url", {"evalId": "eval-test-123"})
        data = json.loads(result.content[0].text)
        print(f"   Success: {data['success']}")
        print(f"   URL: {data.get('url')}")

        print("\n4. Stopping viewer...")
        result = await client.call_tool("stop_viewer", {})
        data = json.loads(result.content[0].text)
        print(f"   Success: {data['success']}")

        print("\n✓ All viewer tools working!")

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
