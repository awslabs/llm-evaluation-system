import asyncio
import json
from src.mcp_client import PromptfooMCPClient

async def main():
    client = PromptfooMCPClient()
    await client.connect()
    
    # Read the docs resource
    result = await client.read_resource("promptfoo://docs/tools")
    docs = json.loads(result.contents[0].text)
    
    # Find generate_dataset and generate_test_cases
    for tool in docs.get("tools", []):
        if "generate" in tool["name"]:
            print(f"\n{tool['name']}:")
            print(f"  Description: {tool['description']}")
            print(f"  Parameters: {tool['parameters']}")
    
    await client.disconnect()

asyncio.run(main())
