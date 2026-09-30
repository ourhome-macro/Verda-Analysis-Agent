"""Bounded Agent-as-Tool calls for cell-scoped research specialists.

The manager owns final audit and publication. A specialist receives only a copied
contract, one cell, and selected passages. This is an application boundary, not a
Python process sandbox: handlers still need ordinary network/LLM timeouts.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Tuple, Type

from pydantic import BaseModel, ValidationError

from app.core import trace


class DelegationError(ValueError):
    """A delegation crossed a schema, scope, capability, or budget boundary."""


def _json_copy(value: Any, max_bytes: int, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if size > max_bytes:
            raise DelegationError(f"{label} exceeds {max_bytes} bytes")
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, DelegationError):
            raise
        raise DelegationError(f"{label} must be finite JSON data") from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _refs(value: Any) -> set:
    """Collect cited evidence IDs, including nested claim/support structures."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_id" and isinstance(item, str):
                found.add(item)
            elif key == "evidence_ids" and isinstance(item, list):
                found.update(ref for ref in item if isinstance(ref, str))
            else:
                found.update(_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_refs(item))
    return found


def _cells(value: Any) -> set:
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "cell_id" and isinstance(item, str):
                found.add(item)
            else:
                found.update(_cells(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_cells(item))
    return found


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    attempt: int
    agent_id: str
    cell_id: str
    contract: Mapping[str, Any]
    cell: Mapping[str, Any]
    passages: Tuple[Mapping[str, Any], ...]
    artifact_refs: Tuple[Mapping[str, Any], ...]
    allowed_evidence_ids: FrozenSet[str]
    capabilities: FrozenSet[str]
    parent_span_id: str
    delegation_depth: int

    def require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise DelegationError(f"capability {capability!r} is not granted")


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    role: str
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Any]
    capabilities: FrozenSet[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DelegationResult:
    tool_name: str
    role: str
    output: Dict[str, Any]
    span_id: str
    parent_span_id: str
    elapsed_ms: int


@dataclass
class DelegationBudget:
    """Shared, atomic call budget. Reuse this instance across nested specialists."""

    max_calls: int = 8
    max_depth: int = 2
    max_input_bytes: int = 16_000
    max_output_bytes: int = 64_000
    max_context_bytes: int = 32_000
    max_passages: int = 28
    max_elapsed_ms: int = 120_000
    calls_used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        limits = (self.max_calls, self.max_depth, self.max_input_bytes,
                  self.max_output_bytes, self.max_context_bytes,
                  self.max_passages, self.max_elapsed_ms)
        if any(type(limit) is not int or limit <= 0 for limit in limits):
            raise ValueError("all delegation limits must be positive integers")

    def reserve(self) -> None:
        with self._lock:
            if self.calls_used >= self.max_calls:
                raise DelegationError("delegation call budget exhausted")
            self.calls_used += 1


# ContextVars preserve the call chain across asyncio.to_thread without sharing it
# with unrelated task workers. The shared budget separately guards concurrent calls.
_STACK: contextvars.ContextVar[Tuple[Tuple[str, str, str], ...]] = contextvars.ContextVar(
    "agent_tool_stack", default=())


def _context_snapshot(source: Any, spec: AgentToolSpec, budget: DelegationBudget,
                      parent_span_id: str, depth: int) -> ToolContext:
    try:
        task_id = source.task_id
        attempt = source.attempt
        agent_id = source.agent_id
        cell_id = source.cell_id
        allowed = frozenset(source.allowed_evidence_ids)
    except (AttributeError, TypeError) as exc:
        raise DelegationError("context requires task_id, attempt, agent_id, cell_id and allowed_evidence_ids") from exc
    if not all(isinstance(x, str) and x.strip() for x in (task_id, agent_id, cell_id)):
        raise DelegationError("task_id, agent_id and cell_id must be nonempty strings")
    if type(attempt) is not int or attempt < 0:
        raise DelegationError("attempt must be a nonnegative integer")
    if any(not isinstance(ref, str) or not ref for ref in allowed):
        raise DelegationError("allowed_evidence_ids must contain nonempty strings")

    granted = getattr(source, "capabilities", None)
    if granted is not None and not spec.capabilities.issubset(frozenset(granted)):
        raise DelegationError("tool requires capabilities absent from agent context")
    allowed_tools = getattr(source, "allowed_tools", None)
    if allowed_tools is not None and spec.name not in allowed_tools:
        raise DelegationError("tool is not allowed in agent context")

    raw = {
        "contract": getattr(source, "contract", {}),
        "cell": getattr(source, "cell", {}),
        "passages": getattr(source, "passages", ()),
        "artifact_refs": getattr(source, "artifact_refs", ()),
    }
    # AgentContext may expose immutable mappings/tuples; normalize only these
    # allowlisted fields. Full report, credentials and unrelated memory stay out.
    def plain(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: plain(value) for key, value in item.items()}
        if isinstance(item, (tuple, list)):
            return [plain(value) for value in item]
        return item

    snapshot = _json_copy(plain(raw), budget.max_context_bytes, "agent context")
    if not isinstance(snapshot["contract"], dict) or not isinstance(snapshot["cell"], dict):
        raise DelegationError("contract and cell must be JSON objects")
    passages = snapshot["passages"]
    artifact_refs = snapshot["artifact_refs"]
    if not isinstance(passages, list) or len(passages) > budget.max_passages:
        raise DelegationError("passage count exceeds delegation limit")
    if not isinstance(artifact_refs, list) or len(artifact_refs) > budget.max_passages:
        raise DelegationError("artifact reference count exceeds delegation limit")
    if any(not isinstance(p, dict) or p.get("evidence_id") not in allowed for p in passages):
        raise DelegationError("context contains a passage outside its evidence allowlist")
    if any(not isinstance(ref, dict) or ref.get("kind") != "Evidence" or
           ref.get("artifact_id") not in allowed for ref in artifact_refs):
        raise DelegationError("context contains an artifact reference outside its evidence allowlist")
    if snapshot["cell"].get("cell_id", cell_id) != cell_id:
        raise DelegationError("context cell_id mismatch")
    return ToolContext(task_id=task_id, attempt=attempt, agent_id=agent_id,
                       cell_id=cell_id, contract=_freeze(snapshot["contract"]),
                       cell=_freeze(snapshot["cell"]), passages=_freeze(passages),
                       artifact_refs=_freeze(artifact_refs),
                       allowed_evidence_ids=allowed, capabilities=spec.capabilities,
                       parent_span_id=parent_span_id, delegation_depth=depth)


class AgentToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, AgentToolSpec] = {}

    def register(self, spec: AgentToolSpec) -> None:
        if not spec.name or not spec.role or spec.name in self._tools:
            raise DelegationError("tool name/role must be nonempty and tool names unique")
        for schema in (spec.input_model, spec.output_model):
            if not isinstance(schema, type) or not issubclass(schema, BaseModel):
                raise TypeError("input_model and output_model must be Pydantic models")
            if schema.model_config.get("extra") != "forbid":
                raise DelegationError("tool schemas must forbid unexpected fields")
        if not callable(spec.handler):
            raise TypeError("tool handler must be callable")
        self._tools[spec.name] = spec

    def invoke(self, tool_name: str, payload: Mapping[str, Any], *, context: Any,
               budget: DelegationBudget) -> DelegationResult:
        spec = self._tools.get(tool_name)
        if spec is None:
            raise DelegationError(f"unregistered tool {tool_name!r}")
        if not isinstance(payload, Mapping):
            raise DelegationError("tool payload must be a JSON object")
        stack = _STACK.get()
        task_id = getattr(context, "task_id", "")
        if stack and stack[-1][0] != task_id:
            raise DelegationError("nested delegation cannot switch tasks")
        if any(name == tool_name for _, name, _ in stack):
            raise DelegationError("delegation loop detected")
        if len(stack) >= budget.max_depth:
            raise DelegationError("delegation depth limit exceeded")
        parent_span_id = stack[-1][2] if stack else ""
        scoped = _context_snapshot(context, spec, budget, parent_span_id, len(stack) + 1)
        data = _json_copy(dict(payload), budget.max_input_bytes, "tool input")
        if _cells(data) - {scoped.cell_id}:
            raise DelegationError("tool input references another cell")
        if _refs(data) - scoped.allowed_evidence_ids:
            raise DelegationError("tool input references evidence outside its context")
        try:
            validated = spec.input_model.model_validate(data)
        except ValidationError as exc:
            raise DelegationError(f"invalid tool input: {exc.errors(include_url=False)}") from exc
        budget.reserve()
        started = time.monotonic()
        start_span = trace.record_manual_span(
            scoped.task_id, scoped.agent_id, "delegation", f"{spec.name}:start",
            detail=f"cell={scoped.cell_id}; role={spec.role}; parent={parent_span_id or '-'}",
            decision="started", evidence_ids=sorted(_refs(data)), model="AgentAsTool")
        span_id = start_span.span_id if start_span else ""
        scoped = ToolContext(**{**scoped.__dict__, "parent_span_id": span_id})
        token = _STACK.set(stack + ((scoped.task_id, spec.name, span_id),))
        try:
            raw_result = spec.handler(validated, scoped)
            if isinstance(raw_result, BaseModel):
                raw_result = raw_result.model_dump(mode="json")
            output = _json_copy(raw_result, budget.max_output_bytes, "tool output")
            if not isinstance(output, dict):
                raise DelegationError("tool output must be a JSON object")
            if _cells(output) - {scoped.cell_id}:
                raise DelegationError("tool output references another cell")
            if "evidence_create" not in spec.capabilities and _refs(output) - scoped.allowed_evidence_ids:
                raise DelegationError("tool output cites evidence outside its context")
            try:
                parsed = spec.output_model.model_validate(output)
            except ValidationError as exc:
                raise DelegationError(f"invalid tool output: {exc.errors(include_url=False)}") from exc
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if elapsed_ms > budget.max_elapsed_ms:
                raise DelegationError("delegation elapsed-time budget exceeded")
            result = parsed.model_dump(mode="json")
            result = _json_copy(result, budget.max_output_bytes, "validated tool output")
            if _cells(result) - {scoped.cell_id}:
                raise DelegationError("validated tool output references another cell")
            if "evidence_create" not in spec.capabilities and _refs(result) - scoped.allowed_evidence_ids:
                raise DelegationError("validated tool output cites evidence outside its context")
            trace.record_manual_span(
                scoped.task_id, scoped.agent_id, "delegation", f"{spec.name}:finish",
                detail=f"cell={scoped.cell_id}; role={spec.role}; parent_span={span_id}",
                decision="completed", evidence_ids=sorted(_refs(result)),
                latency_ms=elapsed_ms, model="AgentAsTool")
            return DelegationResult(spec.name, spec.role, result, span_id,
                                    parent_span_id, elapsed_ms)
        except Exception as exc:
            trace.record_manual_span(
                scoped.task_id, scoped.agent_id, "delegation", f"{spec.name}:finish",
                detail=f"cell={scoped.cell_id}; role={spec.role}; parent_span={span_id}",
                decision=f"failed: {type(exc).__name__}",
                latency_ms=round((time.monotonic() - started) * 1000), model="AgentAsTool")
            raise
        finally:
            _STACK.reset(token)
