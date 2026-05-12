"""LangChain tool-calling agent on Amazon Bedrock.

Uses the modern LangChain 1.x / LangGraph pattern: create_react_agent
binds tools to a ChatBedrockConverse model. Math tools so the eval can
ground-truth verify the outputs.
"""

import sys

from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent


@tool
def add(a: float, b: float) -> float:
    """Return a + b."""
    return a + b


@tool
def subtract(a: float, b: float) -> float:
    """Return a - b."""
    return a - b


@tool
def multiply(a: float, b: float) -> float:
    """Return a * b."""
    return a * b


@tool
def divide(a: float, b: float) -> float:
    """Return a / b. Raises on division by zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


@tool
def power(base: float, exponent: float) -> float:
    """Return base raised to the exponent."""
    return base ** exponent


llm = ChatBedrockConverse(
    model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    region_name="us-west-2",
    temperature=0,
    max_tokens=1024,
)

_agent = create_react_agent(
    llm,
    tools=[add, subtract, multiply, divide, power],
    prompt=(
        "You are a precise math assistant. For every arithmetic step, "
        "call the provided tools instead of computing the answer yourself. "
        "Reply with the final numeric answer only, no explanation."
    ),
)


def run_agent(prompt_text: str) -> str:
    """Entry point used by eval-mcp."""
    result = _agent.invoke({"messages": [{"role": "user", "content": prompt_text}]})
    return result["messages"][-1].content


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "What is (7 * 8) + 3?"
    print(run_agent(q))
