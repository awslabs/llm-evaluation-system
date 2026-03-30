#!/usr/bin/env python3
"""Test generate_test_cases specifically."""

import asyncio
from src.mcp_client import PromptfooMCPClient


async def test_generate_test_cases():
    """Test generate_test_cases with Bedrock."""
    print("=" * 80)
    print("Test: generate_test_cases with Bedrock")
    print("=" * 80)

    client = PromptfooMCPClient()

    try:
        await client.connect()
        print("✓ Connected")

        # Test with a prompt that has variables
        print("\n1. Calling generate_test_cases...")
        result = await client.call_tool(
            "generate_test_cases",
            {
                "prompt": "Translate to French: {{text}}",
                "numTestCases": 3,
            }
        )

        print("\n2. Result:")
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
    success = asyncio.run(test_generate_test_cases())
    print("\n" + "=" * 80)
    print(f"Test: {'✓ PASS' if success else '✗ FAIL'}")
    print("=" * 80)
