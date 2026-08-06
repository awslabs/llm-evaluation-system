"""Shared multi-judge (jury) mechanics.

Single home for what every jury in this codebase does identically, regardless
of what it judges:

- normalize a caller-supplied judge selection into an ordered, deduped list
- fan a prompt out to N judges, excluding judges that error or return
  unparseable output from every downstream denominator (a broken judge must
  never register as a "fail" vote)
- tally votes: strict majority (ties fail) for boolean verdicts, fraction for
  averaged scores

What is deliberately NOT here: prompts, response parsing, and aggregation
*choice*. Those differ per instrument — the generated-config jury scores one
(question, answer, golden) triple per call and averages per-criterion
fractions; the aiwf benchmark jury sends each judge a whole 30-turn transcript
(upstream's verbatim rubric) and majority-votes per turn. Each caller supplies
its own ``call`` coroutine and picks its tally; the mechanics live here once.
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from eval_mcp.core.judge_config import JUDGE_MODELS


def normalize_judges(
    judge_model: Optional[str] = None,
    judge_models: Optional[Union[str, List[str]]] = None,
    default: Optional[str] = None,
) -> List[str]:
    """Normalize the (single, list) judge parameters into an ordered, deduped list.

    ``judge_models`` wins when both are set (it's the superset form) and
    accepts either a real list or a comma-separated string — the latter is how
    it survives Inspect's ``-T judge_models=a,b`` CLI boundary, where the value
    arrives as one YAML scalar. Model ids never contain commas.
    """
    models: List[str] = []
    if judge_models:
        raw = (
            judge_models.split(",")
            if isinstance(judge_models, str)
            else list(judge_models)
        )
        models = [str(m).strip() for m in raw if str(m).strip()]
    elif judge_model:
        models = [judge_model]
    if not models:
        models = [default or JUDGE_MODELS["claude"]]
    seen = set()
    deduped: List[str] = []
    for m in models:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    return deduped


async def collect_verdicts(
    judges: Dict[str, str],
    call: Callable[[str, str], Awaitable[Tuple[Optional[Any], Optional[str]]]],
    parallel: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Tuple[str, str]]]:
    """Run ``call(label, model_id)`` for every judge; split successes from failures.

    ``call`` returns ``(verdict, error)`` — exactly one non-None. Failed judges
    land in the second dict as ``(kind, message)`` where kind is ``"exception"``
    (the call raised — model unreachable, throttled, …) or ``"unparseable"``
    (the judge answered but the verdict couldn't be extracted). Both existing
    juries report those two failure modes differently, so the kernel keeps them
    apart. Either way a failed judge MUST be excluded from every vote
    denominator by the caller; it is not an all-False vote.

    ``parallel`` is a behavioural choice, not an optimization detail: parallel
    fan-out changes throttling characteristics (a burst of judge calls can trip
    Bedrock rate limits and turn into judge exclusions, which moves scores).
    Callers that were verified sequential stay sequential.
    """

    async def one(
        label: str, model_id: str
    ) -> Tuple[str, Optional[Any], Optional[Tuple[str, str]]]:
        try:
            verdict, err = await call(label, model_id)
        except Exception as e:
            return label, None, ("exception", str(e))
        if verdict is None:
            return label, None, ("unparseable", err or "no verdict returned")
        return label, verdict, None

    if parallel:
        results = await asyncio.gather(*(one(l, m) for l, m in judges.items()))
    else:
        results = [await one(l, m) for l, m in judges.items()]

    verdicts = {label: v for label, v, err in results if v is not None}
    errors = {label: err for label, v, err in results if err is not None}
    return verdicts, errors


def majority_passes(yes: int, total: int) -> bool:
    """Strict majority: STRICTLY more than half pass. A tie fails, so an even
    jury is stricter, not random. With one judge this is the identity
    (1/1 > 1/2), so a single-judge path flows through unchanged."""
    return yes * 2 > total


def fraction(votes: List[int]) -> float:
    """Fraction of judges that passed — the averaged (non-collapsing) tally."""
    return sum(votes) / len(votes) if votes else 0.0
