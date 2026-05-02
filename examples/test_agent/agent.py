"""Multi-tool agent with sub-agents for testing pipeline evaluation.

A research assistant that:
1. Routes queries to specialized sub-agents (math, search, summarize)
2. Each sub-agent has its own tools
3. Orchestrator combines results

Uses OpenAI SDK with function calling via the proxy.
"""

import json
import os
import sys

from openai import OpenAI

ORCHESTRATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "math_agent",
            "description": "Delegate to math sub-agent for calculations. Use for any arithmetic, algebra, or numerical computation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The math problem to solve"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_agent",
            "description": "Delegate to search sub-agent for factual lookups. Use for questions about facts, definitions, or knowledge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_agent",
            "description": "Delegate to summarize sub-agent to condense information. Use when you need to combine or shorten multiple pieces of information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to summarize"},
                    "max_words": {"type": "integer", "description": "Maximum words in summary", "default": 50},
                },
                "required": ["text"],
            },
        },
    },
]

MATH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a mathematical expression",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Math expression to evaluate, e.g. '2 + 3 * 4'"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_units",
            "description": "Convert between units",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "number", "description": "The value to convert"},
                    "from_unit": {"type": "string", "description": "Source unit"},
                    "to_unit": {"type": "string", "description": "Target unit"},
                },
                "required": ["value", "from_unit", "to_unit"],
            },
        },
    },
]

SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_definition",
            "description": "Look up the definition of a term or concept",
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "Term to define"},
                },
                "required": ["term"],
            },
        },
    },
]


def execute_math_tool(name, args):
    if name == "calculate":
        try:
            result = eval(args["expression"], {"__builtins__": {}})
            return str(result)
        except Exception as e:
            return f"Error: {e}"
    elif name == "convert_units":
        conversions = {
            ("km", "miles"): 0.621371,
            ("miles", "km"): 1.60934,
            ("kg", "lbs"): 2.20462,
            ("lbs", "kg"): 0.453592,
            ("celsius", "fahrenheit"): lambda v: v * 9/5 + 32,
            ("fahrenheit", "celsius"): lambda v: (v - 32) * 5/9,
            ("meters", "feet"): 3.28084,
            ("feet", "meters"): 0.3048,
        }
        key = (args["from_unit"].lower(), args["to_unit"].lower())
        if key in conversions:
            factor = conversions[key]
            if callable(factor):
                return str(round(factor(args["value"]), 2))
            return str(round(args["value"] * factor, 2))
        return f"Unknown conversion: {args['from_unit']} to {args['to_unit']}"
    return "Unknown tool"


def execute_search_tool(name, args):
    if name == "web_search":
        return f"Search results for '{args['query']}': [Simulated search results would appear here with relevant facts]"
    elif name == "lookup_definition":
        definitions = {
            "photosynthesis": "The process by which green plants use sunlight to synthesize nutrients from carbon dioxide and water.",
            "algorithm": "A step-by-step procedure for solving a problem or accomplishing a task.",
            "quantum computing": "Computing that uses quantum-mechanical phenomena such as superposition and entanglement to perform operations on data.",
            "machine learning": "A subset of AI where systems learn and improve from experience without being explicitly programmed.",
        }
        term = args["term"].lower()
        for key, definition in definitions.items():
            if key in term:
                return definition
        return f"Definition of '{args['term']}': A concept or term in its respective domain."
    return "Unknown tool"


def run_subagent(client, agent_type, query_or_text, max_words=None):
    """Run a sub-agent with its own tools and return the result."""
    if agent_type == "math_agent":
        messages = [
            {"role": "system", "content": "You are a math specialist. Use the calculate tool for computations and convert_units for unit conversions. Return only the final answer."},
            {"role": "user", "content": query_or_text},
        ]
        tools = MATH_TOOLS
        execute_fn = execute_math_tool
    elif agent_type == "search_agent":
        messages = [
            {"role": "system", "content": "You are a research specialist. Use web_search for general queries and lookup_definition for term definitions. Provide concise, factual answers."},
            {"role": "user", "content": query_or_text},
        ]
        tools = SEARCH_TOOLS
        execute_fn = execute_search_tool
    elif agent_type == "summarize_agent":
        word_limit = max_words or 50
        messages = [
            {"role": "system", "content": f"Summarize the following text in {word_limit} words or fewer. Be concise and capture the key points."},
            {"role": "user", "content": query_or_text},
        ]
        tools = []
        execute_fn = None
    else:
        return f"Unknown agent: {agent_type}"

    # Sub-agent loop
    for _ in range(5):
        response = client.chat.completions.create(
            model="inspect",
            messages=messages,
            tools=tools if tools else None,
            tool_choice="auto" if tools else None,
        )

        choice = response.choices[0]
        if choice.finish_reason == "stop":
            return choice.message.content

        if choice.message.tool_calls and execute_fn:
            messages.append(choice.message)
            for tc in choice.message.tool_calls:
                args = json.loads(tc.function.arguments)
                result = execute_fn(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })
        else:
            return choice.message.content or ""

    return "Sub-agent exceeded max iterations"


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "What is 2+2?"

    client = OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:13131/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
    )

    # Orchestrator loop
    messages = [
        {"role": "system", "content": "You are a research assistant orchestrator. Route queries to the appropriate sub-agent:\n- math_agent: for calculations, arithmetic, unit conversions\n- search_agent: for factual questions, definitions, lookups\n- summarize_agent: for condensing information\n\nYou may call multiple agents and combine their results. Always delegate to a sub-agent rather than answering directly."},
        {"role": "user", "content": prompt},
    ]

    for _ in range(10):
        response = client.chat.completions.create(
            model="inspect",
            messages=messages,
            tools=ORCHESTRATOR_TOOLS,
            tool_choice="auto",
        )

        choice = response.choices[0]
        if choice.finish_reason == "stop":
            print(choice.message.content)
            break

        if choice.message.tool_calls:
            messages.append(choice.message)
            for tc in choice.message.tool_calls:
                args = json.loads(tc.function.arguments)
                agent_type = tc.function.name

                if agent_type == "summarize_agent":
                    result = run_subagent(client, agent_type, args["text"], args.get("max_words"))
                else:
                    result = run_subagent(client, agent_type, args["query"])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })
        else:
            print(choice.message.content or "")
            break


if __name__ == "__main__":
    main()
