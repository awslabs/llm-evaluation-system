"""AI Engineer World's Fair multi-turn benchmark (text mode).

Ported from https://github.com/kwindla/aiewf-eval (MIT). Upstream runs the
conversation through a Pipecat pipeline and supports text, realtime-audio and
speech-to-speech models; this port covers **text mode only**. The dataset is
shared between the two — same 30 turns, same 5 tools, same knowledge bases —
so what we run is the same benchmark, minus the audio transport.

Deliberately not ported:

- The four Pipecat pipelines (``realtime``/``nova_sonic``/``grok_realtime``/
  ``audio_in``) and the audio transports. Inspect has no duplex-audio
  transport, so supporting speech models would mean embedding upstream's
  runner rather than writing an Inspect task.
- ``benchmarks/_shared/audio/`` (31 WAVs, 5.9 MB) — the spoken form of each
  turn, used only by those pipelines.
- The ``turn_taking`` judge dimension, which is derived from WAV timing
  analysis (Silero VAD + 2 kHz tag detection). Undefined without audio.

What that leaves is upstream's three judged dimensions — ``tool_use_correct``,
``instruction_following``, ``kb_grounding`` — which is what its text-mode
results table reports.
"""

from eval_mcp.benchmarks.aiwf.data_loader import (
    AIWF_TASKS,
    Turn,
    knowledge_base,
    system_prompt,
    tool_defs,
    turns,
)

__all__ = [
    "AIWF_TASKS",
    "Turn",
    "knowledge_base",
    "system_prompt",
    "tool_defs",
    "turns",
]
