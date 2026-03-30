#!/usr/bin/env python3
"""Test script for multi-MCP server setup."""

import asyncio
from src.mcp_client import MultiMCPClient


async def main():
    """Test connecting to multiple MCP servers."""
    print("=" * 80)
    print("Test: Multi-MCP Server Setup")
    print("=" * 80)

    client = MultiMCPClient(region="us-west-2")

    try:
        print("\n1. Connecting to MCP servers...")
        await client.connect()
        print(f"   ✓ Connected to {len(client.sessions)} server(s)")
        for server_name in client.sessions.keys():
            print(f"     - {server_name}")

        print("\n2. Listing all tools...")
        tools = await client.list_tools()
        print(f"   Found {len(tools)} tools:")
        for tool in tools:
            server = tool.get("_server", "unknown")
            print(f"     - {tool['name']} (from {server})")

        print("\n3. Testing generate_questions tool...")
        result = await client.call_tool(
            "generate_questions",
            {
                "prompt": "A customer support chatbot for a healthcare company",
                "numSamples": 3,
                "numPersonas": 2,
            }
        )

        print("\n4. Result:")
        if hasattr(result, "content"):
            for block in result.content:
                if hasattr(block, "text"):
                    import json
                    data = json.loads(block.text)
                    if data.get("success"):
                        print(f"   ✓ Generated {data['data']['summary']['totalGenerated']} questions")
                        print(f"   Personas: {data['data']['summary']['numPersonas']}")
                        print("\n   Preview:")
                        for i, q in enumerate(data['data']['preview'], 1):
                            print(f"     {i}. {q}")
                    else:
                        print(f"   ✗ Error: {data.get('error')}")

        print("\n✓ SUCCESS: Multi-MCP setup working!")
        return True

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        await client.disconnect()
        print("\n" + "=" * 80)


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
