"""Simple agent that calls Bedrock directly via boto3.

Simulates a real-world agent (like Strands, LangChain with Bedrock, etc.)
that uses boto3 Converse API. No OpenAI SDK, no special configuration.

This is what a user's agent looks like — our eval captures its calls
without any modification to this file.
"""

import json
import os
import sys

import boto3

TOOLS = [
    {
        "toolSpec": {
            "name": "calculate",
            "description": "Evaluate a mathematical expression",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string", "description": "Math expression, e.g. '25 * 4'"},
                    },
                    "required": ["expression"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search",
            "description": "Search for factual information about a topic",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for"},
                    },
                    "required": ["query"],
                }
            },
        }
    },
]

KNOWLEDGE_BASE = {
    "machine learning": "Machine learning is a subset of artificial intelligence where systems learn patterns from data to make predictions or decisions without being explicitly programmed.",
    "photosynthesis": "Photosynthesis is the process by which green plants convert sunlight, water, and carbon dioxide into glucose and oxygen.",
    "gravity": "Gravity is a fundamental force of nature that attracts objects with mass toward each other. On Earth, it accelerates objects at approximately 9.8 m/s squared.",
    "python": "Python is a high-level, interpreted programming language known for its readability and versatility.",
}


def execute_tool(name: str, tool_input: dict) -> str:
    if name == "calculate":
        try:
            result = eval(tool_input["expression"], {"__builtins__": {}})
            return str(result)
        except Exception as e:
            return f"Error: {e}"
    elif name == "search":
        query = tool_input["query"].lower()
        for key, value in KNOWLEDGE_BASE.items():
            if key in query:
                return value
        return f"Information about '{tool_input['query']}': A topic with various applications."
    return f"Unknown tool: {name}"


def run_agent(prompt: str) -> str:
    """Run the agent — calls Bedrock directly via boto3 Converse."""
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    client = boto3.client("bedrock-runtime", region_name=region)
    model_id = os.environ.get("AGENT_MODEL", "us.anthropic.claude-sonnet-4-6")

    messages = [
        {"role": "user", "content": [{"text": prompt}]},
    ]

    for _ in range(5):
        response = client.converse(
            modelId=model_id,
            messages=messages,
            system=[{"text": "You are a helpful assistant. Use the calculate tool for math and the search tool for factual questions. Always use tools when applicable."}],
            toolConfig={"tools": TOOLS},
            inferenceConfig={"maxTokens": 1024},
        )

        output_msg = response["output"]["message"]
        stop_reason = response["stopReason"]

        if stop_reason == "end_turn":
            # Extract text from response
            text_parts = [b["text"] for b in output_msg["content"] if "text" in b]
            return "\n".join(text_parts)

        if stop_reason == "tool_use":
            # Add assistant message to history
            messages.append(output_msg)

            # Execute tool calls and add results
            tool_results = []
            for block in output_msg["content"]:
                if "toolUse" in block:
                    tool_use = block["toolUse"]
                    result = execute_tool(tool_use["name"], tool_use["input"])
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use["toolUseId"],
                            "content": [{"text": result}],
                        }
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            text_parts = [b["text"] for b in output_msg["content"] if "text" in b]
            return "\n".join(text_parts) if text_parts else ""

    return "Agent exceeded max iterations"


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "What is 2+2?"
    print(run_agent(prompt))
