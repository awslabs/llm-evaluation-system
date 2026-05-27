"""Pure-logic smoke test for the static_output_solver.

We can't fully simulate Inspect's TaskState in a unit test, but the
solver's behavior is narrow: read ``state.metadata["actual_output"]``,
build a ``ModelOutput.from_content``, set ``state.output``, return.
Stub out the bits we need.
"""

import pytest

from eval_mcp.solvers.static_output import STATIC_MODEL_ID, static_output_solver


class _Stub:
    """Minimal stand-in for TaskState.

    Inspect's TaskState has dozens of fields; the solver only touches
    ``metadata`` (read) and ``output`` (write). A duck-typed class with
    those two attributes is enough for a unit test.
    """

    def __init__(self, metadata):
        self.metadata = metadata
        self.output = None


async def _noop_generate(state):
    """Stand-in for the ``generate`` arg the solver receives. Should
    only be called when actual_output is absent. We mark on the state
    so the test can assert behaviour."""
    state.output = "FELL_THROUGH_TO_GENERATE"
    return state


@pytest.mark.asyncio
async def test_sets_output_from_metadata() -> None:
    solver = static_output_solver()
    state = _Stub(metadata={"actual_output": "pre-generated reply"})
    result = await solver(state, _noop_generate)
    # state.output is a ModelOutput; its .completion is the content
    assert result.output is not None
    assert result.output.completion == "pre-generated reply"
    assert result.output.model == STATIC_MODEL_ID


@pytest.mark.asyncio
async def test_falls_through_to_generate_when_missing() -> None:
    solver = static_output_solver()
    state = _Stub(metadata={})
    result = await solver(state, _noop_generate)
    assert result.output == "FELL_THROUGH_TO_GENERATE"


@pytest.mark.asyncio
async def test_falls_through_when_empty() -> None:
    solver = static_output_solver()
    state = _Stub(metadata={"actual_output": ""})
    result = await solver(state, _noop_generate)
    assert result.output == "FELL_THROUGH_TO_GENERATE"


@pytest.mark.asyncio
async def test_handles_none_metadata() -> None:
    solver = static_output_solver()
    state = _Stub(metadata=None)
    result = await solver(state, _noop_generate)
    assert result.output == "FELL_THROUGH_TO_GENERATE"
