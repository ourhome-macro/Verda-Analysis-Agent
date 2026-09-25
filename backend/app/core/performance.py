"""Request-scoped timing and usage counters for research runs."""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT: ContextVar[str] = ContextVar("research_performance_task", default="")
_LOCK = threading.RLock()
_RUNS: dict[str, dict] = {}


def percentile(values, fraction: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 1)


@contextmanager
def use_task(task_id: str):
    token = _CURRENT.set(task_id)
    with _LOCK:
        _RUNS[task_id] = {"started": {}, "stages": defaultdict(float),
                          "calls": defaultdict(list), "cache": defaultdict(int),
                          "tokens": defaultdict(int), "retries": defaultdict(int)}
    try:
        yield
    finally:
        _CURRENT.reset(token)


def on_node_update(task_id: str, data: dict):
    node, status = data.get("node"), data.get("status")
    if not node:
        return
    now = time.monotonic()
    with _LOCK:
        run = _RUNS.get(task_id)
        if run is None:
            return
        if status in ("working", "rework"):
            run["started"].setdefault(node, now)
        elif status == "done":
            began = run["started"].pop(node, None)
            if began is not None:
                run["stages"][node] += now - began


def record(kind: str, provider: str, elapsed_ms: float, *, cache_hit=False,
           prompt_tokens=0, completion_tokens=0, cached_tokens=0, retry=False):
    task_id = _CURRENT.get()
    if not task_id:
        return
    with _LOCK:
        run = _RUNS.get(task_id)
        if run is None:
            return
        run["calls"][kind].append(max(0.0, float(elapsed_ms)))
        run["calls"][f"{kind}:{provider}"].append(max(0.0, float(elapsed_ms)))
        if cache_hit:
            run["cache"][kind] += 1
        if retry:
            run["retries"][kind] += 1
        run["tokens"]["input"] += int(prompt_tokens or 0)
        run["tokens"]["output"] += int(completion_tokens or 0)
        run["tokens"]["provider_cache_hit"] += int(cached_tokens or 0)


def snapshot(task_id: str) -> dict:
    with _LOCK:
        run = _RUNS.get(task_id)
        if run is None:
            return {}
        calls = {kind: {"count": len(values), "total_ms": round(sum(values), 1),
                        "p50_ms": percentile(values, 0.5), "p95_ms": percentile(values, 0.95),
                        "samples_ms": [round(v, 1) for v in values[:500]]}
                 for kind, values in run["calls"].items()}
        return {"stage_seconds": {k: round(v, 3) for k, v in run["stages"].items()},
                "calls": calls, "cache_hits": dict(run["cache"]),
                "tokens": dict(run["tokens"]), "retries": dict(run["retries"])}


def cleanup(task_id: str):
    with _LOCK:
        _RUNS.pop(task_id, None)
