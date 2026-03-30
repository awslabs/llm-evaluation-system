#!/usr/bin/env python3
"""Test generate_qa_pairs MCP tool."""

import asyncio
import json
from src.mcp_client import MultiMCPClient


async def main():
    print("=" * 80)
    print("Test: Generate QA Pairs Tool")
    print("=" * 80)

    client = MultiMCPClient()

    try:
        print("\n1. Connecting...")
        await client.connect()
        print(f"   ✓ Connected to {len(client.sessions)} servers")

        print("\n2. Listing tools...")
        tools = await client.list_tools()
        tool_names = [t["name"] for t in tools]
        print(f"   Available tools: {', '.join(tool_names)}")

        if "generate_qa_pairs" not in tool_names:
            print("   ✗ ERROR: generate_qa_pairs tool not found!")
            return

        print("\n3. Generating QA pairs...")
        result = await client.call_tool(
            "generate_qa_pairs",
            {
                "prompt": "A customer support chatbot for a cloud storage service",
                "numSamples": 5,
                "numPersonas": 3,
                "outputPath": "test-qa-pairs.yaml",
            },
        )

        data = json.loads(result.content[0].text)

        if data.get("success"):
            print(f"   ✓ Generated {data['data']['summary']['totalGenerated']} QA pairs")
            print(f"   ✓ Used {data['data']['summary']['numPersonas']} personas")
            print(f"   ✓ Saved to: {data['data']['summary']['outputPath']}")

            print("\n4. Preview of generated QA pairs:")
            for i, test_case in enumerate(data["data"]["preview"], 1):
                print(f"\n   Test Case {i}:")
                print(f"   Q: {test_case['vars']['question']}")
                print(f"   A: {test_case['vars']['golden_answer'][:100]}...")
                print(f"   Assertion: {test_case['assert'][0]['type']}")

            print("\n✓ All tests passed!")
        else:
            print(f"   ✗ ERROR: {data.get('error')}")

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
