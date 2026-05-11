"""Multi-agent orchestrator using Strands Agents SDK.

Based on strands-agents/samples (Apache-2.0 license).
Routes queries to specialized sub-agents: research, product recommendation, trip planning.

This agent calls Bedrock directly via boto3 (no OpenAI SDK).
Our eval captures all Bedrock calls without any modification to this file.
"""

import sys

from strands import Agent, tool


RESEARCH_ASSISTANT_PROMPT = """You are a specialized research assistant. Focus only on providing
factual, well-sourced information in response to research questions.
Always cite your sources when possible."""


@tool
def research_assistant(query: str) -> str:
    """Process and respond to research-related queries.

    Args:
        query: A research question requiring factual information

    Returns:
        A detailed research answer with citations
    """
    try:
        research_agent = Agent(
            model="us.anthropic.claude-sonnet-4-6",
            system_prompt=RESEARCH_ASSISTANT_PROMPT,
        )
        response = research_agent(query)
        return str(response)
    except Exception as e:
        return f"Error in research assistant: {str(e)}"


@tool
def product_recommendation_assistant(query: str) -> str:
    """Handle product recommendation queries by suggesting appropriate products.

    Args:
        query: A product inquiry with user preferences

    Returns:
        Personalized product recommendations with reasoning
    """
    try:
        product_agent = Agent(
            model="us.anthropic.claude-sonnet-4-6",
            system_prompt="""You are a specialized product recommendation assistant.
            Provide personalized product suggestions based on user preferences.""",
        )
        response = product_agent(query)
        return str(response)
    except Exception as e:
        return f"Error in product recommendation: {str(e)}"


@tool
def trip_planning_assistant(query: str) -> str:
    """Create travel itineraries and provide travel advice.

    Args:
        query: A travel planning request with destination and preferences

    Returns:
        A detailed travel itinerary or travel advice
    """
    try:
        travel_agent = Agent(
            model="us.anthropic.claude-sonnet-4-6",
            system_prompt="""You are a specialized travel planning assistant.
            Create detailed travel itineraries based on user preferences.""",
        )
        response = travel_agent(query)
        return str(response)
    except Exception as e:
        return f"Error in trip planning: {str(e)}"


ORCHESTRATOR_PROMPT = """You are an assistant that routes queries to specialized agents:
- For research questions and factual information → Use the research_assistant tool
- For product recommendations and shopping advice → Use the product_recommendation_assistant tool
- For travel planning and itineraries → Use the trip_planning_assistant tool
- For simple questions not requiring specialized knowledge → Answer directly

Always select the most appropriate tool based on the user's query."""


orchestrator = Agent(
    model="us.anthropic.claude-sonnet-4-6",
    system_prompt=ORCHESTRATOR_PROMPT,
    tools=[research_assistant, product_recommendation_assistant, trip_planning_assistant],
)


def run_agent(prompt: str) -> str:
    """Run the orchestrator agent and return the response."""
    response = orchestrator(prompt)
    return str(response)


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "What is machine learning?"
    print(run_agent(prompt))
