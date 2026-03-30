#!/usr/bin/env python3
"""Test generate_dataset specifically."""

import asyncio
import json
from src.mcp_client import PromptfooMCPClient


async def test_generate_dataset():
    """Test generate_dataset with Bedrock."""
    print("=" * 80)
    print("Test: generate_dataset with Bedrock")
    print("=" * 80)

    client = PromptfooMCPClient()

    try:
        await client.connect()
        print("✓ Connected")

        # First, let's see the tool schema
        print("\n1. Checking tool schema...")
        result = await client.read_resource("promptfoo://docs/tools")
        docs = json.loads(result.contents[0].text)

        for tool in docs.get("tools", []):
            if tool["name"] == "generate_dataset":
                print(f"\nTool: {tool['name']}")
                print(f"Description: {tool['description']}")
                print(f"\nParameters schema:")
                print(json.dumps(tool["parameters"], indent=2))
                break

        # Try WITHOUT specifying provider - let it use promptfooconfig.yaml default
        print("\n2. Calling generate_dataset without provider (use config default)...")
        result = await client.call_tool(
            "generate_dataset",
            {
                "prompt": "Test prompt for customer support chatbot",
                "numSamples": 3,
                # No provider parameter - should use defaultTest.options.provider from config
            }
        )

        print("\n3. Result:")
        if hasattr(result, "content"):
            for block in result.content:
                if hasattr(block, "text"):
                    print(block.text)
            return True
        return False

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(test_generate_dataset())
