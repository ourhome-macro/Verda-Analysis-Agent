"""Process-wide outbound token bucket plus in-flight limit, keyed by credential."""
from __future__ import annotations

import hashlib
import threading
import time
from contextlib import contextmanager

_COND = threading.Condition()
_STATE: dict[str, dict] = {}


def scope(provider: str, base_url: str, credential: str) -> str:
    raw = f"{provider}\0{base_url}\0{credential}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def permit(key: str, *, per_second: float, max_in_flight: int, max_wait: float = 60.0):
    rate = max(0.01, float(per_second))
    slots = max(1, int(max_in_flight))
    deadline = time.monotonic() + max(0.0, max_wait)
    with _COND:
        if len(_STATE) > 4096:
            now = time.monotonic()
            for stale_key in list(_STATE):
                item = _STATE[stale_key]
                if item["in_flight"] == 0 and now - item["updated"] > 3600:
                    del _STATE[stale_key]
        state = _STATE.setdefault(key, {"tokens": 1.0, "updated": time.monotonic(),
                                       "in_flight": 0, "cooldown": 0.0})
        while True:
            now = time.monotonic()
            state["tokens"] = min(max(1.0, rate), state["tokens"] + (now - state["updated"]) * rate)
            state["updated"] = now
            if now >= state["cooldown"] and state["tokens"] >= 1 and state["in_flight"] < slots:
                state["tokens"] -= 1
                state["in_flight"] += 1
                break
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError("上游请求排队超时")
            until_token = max(0.01, (1 - state["tokens"]) / rate)
            until_cooldown = max(0.0, state["cooldown"] - now)
            _COND.wait(min(remaining, max(0.05, until_token, until_cooldown)))
    try:
        yield
    finally:
        with _COND:
            state["in_flight"] = max(0, state["in_flight"] - 1)
            _COND.notify_all()


def cool_down(key: str, seconds: float):
    with _COND:
        state = _STATE.setdefault(key, {"tokens": 1.0, "updated": time.monotonic(),
                                       "in_flight": 0, "cooldown": 0.0})
        state["cooldown"] = max(state["cooldown"], time.monotonic() + max(0.0, seconds))
        _COND.notify_all()
