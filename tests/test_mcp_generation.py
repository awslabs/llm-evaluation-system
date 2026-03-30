#!/usr/bin/env python3
"""Test script for MCP operations with Bedrock."""

import asyncio
import sys
from src.mcp_client import PromptfooMCPClient


async def test_list_evaluations():
    """Test listing evaluations."""
    print("=" * 80)
    print("Test 1: List Evaluations")
    print("=" * 80)

    # Check AWS credentials
    import os
    print("\n0. Checking environment...")
    print(f"   AWS_PROFILE: {os.environ.get('AWS_PROFILE', 'not set')}")
    print(f"   AWS_REGION: {os.environ.get('AWS_REGION', 'not set')}")
    print(f"   Working dir: {os.getcwd()}")
    print(f"   Config exists: {os.path.exists('promptfooconfig.yaml')}")

    client = PromptfooMCPClient()

    try:
        print("\n1. Connecting to MCP server...")
        await client.connect()
        print("   ✓ Connected")

        print("\n2. Calling list_evaluations...")
        result = await client.call_tool("list_evaluations", {})

        print("\n3. Result:")
        if hasattr(result, "content"):
            for block in result.content:
                if hasattr(block, "text"):
                    print(f"   {block.text[:500]}")
            print("   ✓ SUCCESS: list_evaluations works!")
            return True
        else:
            print(f"   ✗ Unexpected format")
            return False

    except Exception as e:
        print(f"\n   ✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        await client.disconnect()
        print("\n" + "=" * 80)


async def test_run_evaluation():
    """Test running an evaluation with Bedrock."""
    print("\n" + "=" * 80)
    print("Test 2: Run Evaluation with Bedrock")
    print("=" * 80)

    client = PromptfooMCPClient()

    try:
        await client.connect()
        print("   ✓ Connected")

        print("\n1. Running evaluation with promptfooconfig.yaml...")
        result = await client.call_tool(
            "run_evaluation",
            {
                "configPath": "promptfooconfig.yaml",
            }
        )

        print("\n2. Result:")
        if hasattr(result, "content"):
            for block in result.content:
                if hasattr(block, "text"):
                    print(f"   {block.text[:500]}")
            return True
        return False

    except Exception as e:
        print(f"   ✗ ERROR: {e}")
        return False
    finally:
        await client.disconnect()


if __name__ == "__main__":
    success1 = asyncio.run(test_list_evaluations())
    success2 = asyncio.run(test_run_evaluation())

    print("\n" + "=" * 80)
    print(f"Test 1 (list): {'✓ PASS' if success1 else '✗ FAIL'}")
    print(f"Test 2 (run):  {'✓ PASS' if success2 else '✗ FAIL'}")
    print("=" * 80)

    sys.exit(0 if (success1 and success2) else 1)
