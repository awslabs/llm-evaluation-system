"""Auto-discovery for bundled benchmarks.

A bundled benchmark is a directory under ``eval_mcp/benchmarks/`` containing:

    <name>/
        eval.yaml     metadata (title, tasks, metrics, source, caveats)
        task.py       Inspect @task functions, one per entry in tasks[]
        data/         vendored datasets + prompts, verbatim from upstream
        NOTICE.md     provenance: upstream URL, license, pinned SHA

Adding one means dropping in that directory. **No Python is edited** — not this
file, not the MCP tool, not the server. The catalog is whatever is on disk, which
is the same contract ``inspect_evals`` uses (it globs ``*/eval.yaml``) and keeps
the two benchmark sources feeling identical to a caller.

The ``eval.yaml`` schema deliberately mirrors ``inspect_evals``' own, so a port
here could later be submitted to their register with minimal change.
"""

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_BENCHMARKS_DIR = Path(__file__).parent


@dataclass(frozen=True)
class TaskEntry:
    """One runnable task within a benchmark."""

    name: str
    description: str = ""
    turns: Optional[int] = None
    approx_input_tokens_per_model: Optional[int] = None


@dataclass(frozen=True)
class BundledBenchmark:
    """One benchmark directory, as described by its ``eval.yaml``."""

    id: str
    path: Path
    title: str
    description: str
    group: str
    version: str
    tasks: List[TaskEntry]
    metrics: List[Dict[str, str]] = field(default_factory=list)
    judge: Dict[str, Any] = field(default_factory=dict)
    source: Dict[str, Any] = field(default_factory=dict)
    caveats: List[str] = field(default_factory=list)

    @property
    def task_file(self) -> Path:
        """Absolute path to the task module.

        Inspect is invoked as ``<abs path>@<task_name>``; absolute works from any
        cwd, which matters because the eval subprocess runs in the user's storage
        directory.
        """
        return (self.path / "task.py").resolve()

    @property
    def task_names(self) -> List[str]:
        return [t.name for t in self.tasks]

    def task(self, name: str) -> Optional[TaskEntry]:
        return next((t for t in self.tasks if t.name == name), None)

    @property
    def headline_metric(self) -> Optional[str]:
        return self.metrics[0]["name"] if self.metrics else None

    @property
    def default_judge(self) -> Optional[str]:
        return self.judge.get("default")

    def summary(self) -> Dict[str, Any]:
        """Compact projection for ``list_*`` output — keep it small."""
        return {
            "id": self.id,
            "title": self.title,
            "category": self.group,
            "tasks": self.task_names,
            "bundled": True,
            "headlineMetric": self.headline_metric,
        }

    def details(self) -> Dict[str, Any]:
        """Everything a caller needs to run it and read the result honestly."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description.strip(),
            "category": self.group,
            "version": self.version,
            "bundled": True,
            "tasks": [
                {
                    "name": t.name,
                    "description": t.description,
                    "turns": t.turns,
                    "approxInputTokensPerModel": t.approx_input_tokens_per_model,
                }
                for t in self.tasks
            ],
            "metrics": self.metrics,
            "headlineMetric": self.headline_metric,
            "judge": self.judge,
            "source": self.source,
            "caveats": self.caveats,
            "runHint": (
                f'run_benchmark(task="{self.task_names[0]}", providers=[...])'
                if self.task_names
                else "No runnable task declared in eval.yaml."
            ),
        }


def _parse(path: Path) -> BundledBenchmark:
    import yaml

    raw = yaml.safe_load((path / "eval.yaml").read_text(encoding="utf-8")) or {}
    tasks = [
        TaskEntry(
            name=t["name"],
            description=t.get("description", ""),
            turns=t.get("turns"),
            approx_input_tokens_per_model=t.get("approx_input_tokens_per_model"),
        )
        for t in (raw.get("tasks") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    return BundledBenchmark(
        id=path.name,
        path=path,
        title=raw.get("title") or path.name,
        description=raw.get("description") or "",
        group=raw.get("group") or "Bundled",
        version=str(raw.get("version") or "1"),
        tasks=tasks,
        metrics=[m for m in (raw.get("metrics") or []) if isinstance(m, dict)],
        judge=raw.get("judge") or {},
        source=raw.get("source") or {},
        caveats=list(raw.get("caveats") or []),
    )


@functools.lru_cache(maxsize=1)
def discover() -> Dict[str, BundledBenchmark]:
    """Every bundled benchmark, keyed by id. Cached — the set is fixed at
    install time (these ship as package data)."""
    found: Dict[str, BundledBenchmark] = {}
    for child in sorted(_BENCHMARKS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        if not (child / "eval.yaml").is_file() or not (child / "task.py").is_file():
            continue
        found[child.name] = _parse(child)
    return found


def all_task_names() -> Dict[str, BundledBenchmark]:
    """Map every runnable task name -> its benchmark.

    Task names are the user-facing handle (``aiwf_medium_context``), matching how
    ``inspect_evals`` tasks are addressed.
    """
    return {
        name: bench for bench in discover().values() for name in bench.task_names
    }


def resolve(task_or_id: str) -> Optional[tuple[BundledBenchmark, str]]:
    """Resolve a task name, or a benchmark id with exactly one task.

    Returns ``(benchmark, task_name)`` or None. Mirrors
    ``tools/benchmarks._resolve_task`` so both catalogs accept the same input.
    """
    by_task = all_task_names()
    if task_or_id in by_task:
        return by_task[task_or_id], task_or_id
    bench = discover().get(task_or_id)
    if bench and len(bench.task_names) == 1:
        return bench, bench.task_names[0]
    return None
