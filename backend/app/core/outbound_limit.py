"""Process-wide, credential-aware outbound admission control."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("uvicorn.error")
_COND = threading.Condition()
_STATE: dict[str, dict] = {}


def scope(provider: str, base_url: str, credential: str) -> str:
    return hashlib.sha256(f"{provider}\0{base_url}\0{credential}".encode()).hexdigest()


def settings_permit(provider: str, base_url: str, credential: str, settings, *,
                    per_second: float, max_in_flight: int, max_wait: float):
    if (per_second <= 0 or max_in_flight <= 0 or max_wait < 0 or
            settings.outbound_global_requests_per_second <= 0 or
            settings.outbound_global_max_in_flight <= 0):
        raise ValueError("出站限流配置必须为正值，队列等待时间不能为负")
    return permit(scope(provider, base_url, credential), per_second=per_second,
                  max_in_flight=max_in_flight, max_wait=max_wait,
                  provider=scope(provider, "", ""),
                  provider_label=provider,
                  provider_per_second=per_second,
                  provider_max_in_flight=max_in_flight,
                  global_per_second=settings.outbound_global_requests_per_second,
                  global_max_in_flight=settings.outbound_global_max_in_flight)


def _state(key: str, now: float) -> dict:
    return _STATE.setdefault(key, {"tokens": 1.0, "updated": now,
                                   "in_flight": 0, "cooldown": 0.0})


@contextmanager
def permit(key: str, *, per_second: float, max_in_flight: int, max_wait: float = 60.0,
           provider: str = "", provider_label: str = "", provider_per_second: float = 0,
           provider_max_in_flight: int = 0, global_per_second: float = 0,
           global_max_in_flight: int = 0):
    """Acquire all configured budgets atomically; never hold a slot while queued."""
    budgets = [(f"credential:{key}", per_second, max_in_flight)]
    if provider:
        budgets.append((f"provider:{provider}", provider_per_second, provider_max_in_flight))
    budgets.append(("global", global_per_second, global_max_in_flight))
    budgets = [(name, float(rate), int(slots)) for name, rate, slots in budgets
               if rate > 0 or slots > 0]
    started = time.monotonic()
    deadline = started + max(0.0, max_wait)
    with _COND:
        if len(_STATE) > 4096:
            for stale_key, item in list(_STATE.items()):
                if item["in_flight"] == 0 and started - item["updated"] > 3600:
                    del _STATE[stale_key]
        while True:
            now = time.monotonic()
            states = []
            delay = 0.0
            for name, rate, slots in budgets:
                state = _state(name, now)
                if rate > 0:
                    state["tokens"] = min(max(1.0, rate), state["tokens"] + (now - state["updated"]) * rate)
                state["updated"] = now
                delay = max(delay, state["cooldown"] - now)
                if rate > 0 and state["tokens"] < 1:
                    delay = max(delay, (1 - state["tokens"]) / rate)
                if slots > 0 and state["in_flight"] >= slots:
                    delay = max(delay, 0.05)
                states.append((state, rate))
            if delay <= 0:
                for state, rate in states:
                    if rate > 0:
                        state["tokens"] -= 1
                    state["in_flight"] += 1
                break
            remaining = deadline - now
            if remaining <= 0:
                log.warning("outbound_queue_timeout provider=%s wait_ms=%d", provider_label or provider,
                            int((now - started) * 1000))
                raise TimeoutError("上游请求排队超时")
            _COND.wait(min(remaining, max(0.01, delay)))
    waited = int((time.monotonic() - started) * 1000)
    if waited >= 10:
        log.info("outbound_admitted provider=%s queue_ms=%d", provider_label or provider, waited)
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        elapsed = int((time.monotonic() - started) * 1000)
        with _COND:
            for state, _ in states:
                state["in_flight"] -= 1
            _COND.notify_all()
        log.info("outbound_complete provider=%s outcome=%s elapsed_ms=%d queue_ms=%d",
                 provider_label or provider, outcome, elapsed, waited)


def cool_down(key: str, seconds: float):
    with _COND:
        state = _state(f"credential:{key}", time.monotonic())
        state["cooldown"] = max(state["cooldown"], time.monotonic() + max(0.0, seconds))
        _COND.notify_all()
