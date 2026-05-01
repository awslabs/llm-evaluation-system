"""Agent eval test: runs a simple agent in a container via sandbox_agent_bridge."""

from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent, sandbox_agent_bridge
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageUser
from inspect_ai.scorer import includes
from inspect_ai.util import sandbox


@agent
def agent_solver() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        async with sandbox_agent_bridge(state, model="inspect") as bridge:
            prompt = ""
            for msg in reversed(state.messages):
                if isinstance(msg, ChatMessageUser):
                    prompt = msg.text
                    break

            result = await sandbox().exec(
                cmd=["python", "agent.py", prompt],
                env={
                    "OPENAI_BASE_URL": f"http://localhost:{bridge.port}/v1",
                    "OPENAI_API_KEY": "not-needed",
                },
                timeout=120,
            )

            if not result.success:
                raise RuntimeError(
                    f"Agent failed (exit {result.returncode}):\nSTDOUT: {result.stdout[:1000]}\nSTDERR: {result.stderr[:1000]}"
                )

        return bridge.state

    return execute


@task
def eval_task():
    return Task(
        dataset=[
            Sample(input="What is 2+2?", target="4"),
            Sample(input="What is the capital of France?", target="Paris"),
            Sample(input="What color is the sky?", target="blue"),
        ],
        solver=agent_solver(),
        scorer=includes(),
        sandbox=("docker", "compose.yaml"),
    )
