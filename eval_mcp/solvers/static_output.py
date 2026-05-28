"""Solver that uses pre-generated outputs instead of calling a model.

Score-only mode: the dataset already carries an ``actual_output`` per
sample (the user ran their candidate system offline and captured the
answers). Inspect AI normally calls a model via ``generate()`` to fill
``state.output``; this solver short-circuits that step by constructing
a ``ModelOutput`` from the dataset value, so downstream scorers see
the exact same shape they'd see after a real model call.

The solver is also safe to drop into a mixed task: if a sample lacks
``actual_output`` it falls through to the normal ``generate(state)``
chain, so a user could in principle compose
``solver=[static_output_solver(), generate()]`` for opportunistic
score-only behaviour per sample. We don't expose that mode through
``create_eval_config`` for v1 — the wrapper requires all-or-none — but
the solver itself doesn't enforce that.
"""

from __future__ import annotations

from inspect_ai.model import ModelOutput
from inspect_ai.solver import solver

# Synthetic model identifier written into the eval log when no real
# model was invoked. The viewer relabels this to ``"pre-generated"`` —
# see ``eval_mcp/core/eval_results.py`` near the agent-image relabel
# block. Don't change the literal without also updating that swap.
STATIC_MODEL_ID = "static/preset"


@solver
def static_output_solver():
    """Set ``state.output`` from ``state.metadata['actual_output']`` and return.

    No model is invoked. Falls through to ``generate(state)`` when the
    metadata field is absent so the solver is composable with normal
    runs. (``create_eval_config`` only wires this in when every sample
    has ``actual_output``, but the solver itself stays general.)
    """
    async def solve(state, generate):
        actual = (state.metadata or {}).get("actual_output")
        if actual is None or actual == "":
            return await generate(state)
        state.output = ModelOutput.from_content(
            model=STATIC_MODEL_ID, content=str(actual)
        )
        return state

    return solve
