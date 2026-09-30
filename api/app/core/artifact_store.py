"""Immutable task artifacts and bounded, agent-local evidence contexts.

Artifacts are shared through explicit versioned reads. An agent receives only
the contract, target cell and whitelisted passages selected for that call.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable

from app.core import db
from app.core.evidence_context import select_evidence


KINDS = frozenset({"Evidence", "Claim", "PlanDelta", "Report"})
_ARTIFACT_RUN: ContextVar[tuple[str, int] | None] = ContextVar(
    "verda_artifact_run", default=None)


@contextmanager
def use_artifact_run(task_id: str, attempt: int):
    """Opt in one runtime attempt to artifact persistence and scoped reads."""
    if not task_id or attempt < 1:
        raise ValueError("task_id and positive attempt are required")
    token = _ARTIFACT_RUN.set((task_id, attempt))
    try:
        yield
    finally:
        _ARTIFACT_RUN.reset(token)


def current_artifact_run() -> tuple[str, int] | None:
    return _ARTIFACT_RUN.get()


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty string of at most 256 characters")
    return value


def _canonical(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise ValueError("artifact payload exceeds 2 MB")
    return encoded


def _validate_scope(task_id: str, attempt: int, cell_id: str) -> None:
    _required_text(task_id, "task_id")
    _required_text(cell_id, "cell_id")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive integer")


def _check_access(conn, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        raise LookupError("task does not exist")
    if not db._owned(conn, "task", task_id):
        raise PermissionError("Task is not accessible")


def _decode(row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    result["source_offsets"] = json.loads(result["source_offsets"])
    return result


def publish_artifact(
    task_id: str, attempt: int, cell_id: str, kind: str, artifact_id: str,
    payload: dict[str, Any], author: str, *, expected_version: int | None = None,
    source_group: str = "", source_offsets: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Append one immutable version; expected_version gives optimistic control.

    Repeating the exact latest payload and metadata is idempotent. A different
    payload always creates a new version; prior versions never change.
    """
    _validate_scope(task_id, attempt, cell_id)
    _required_text(artifact_id, "artifact_id")
    _required_text(author, "author")
    if kind not in KINDS:
        raise ValueError(f"unsupported artifact kind: {kind}")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dictionary")
    if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
        raise ValueError("expected_version must be nonnegative")
    offsets = source_offsets or {}
    if not isinstance(offsets, dict) or any(
        k not in {"start", "end"} or type(v) is not int or v < 0
        for k, v in offsets.items()
    ) or ("start" in offsets and "end" in offsets and offsets["end"] < offsets["start"]):
        raise ValueError("source_offsets must contain nonnegative start/end positions")
    if not isinstance(source_group, str) or len(source_group) > 512:
        raise ValueError("source_group is invalid")
    encoded = _canonical(payload)
    offsets_json = _canonical(offsets)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with db._LOCK:
        conn = db._connect()
        _check_access(conn, task_id)
        latest = conn.execute("""SELECT * FROM artifact_versions
            WHERE task_id=? AND attempt=? AND kind=? AND artifact_id=?
            ORDER BY version DESC LIMIT 1""",
            (task_id, attempt, kind, artifact_id)).fetchone()
        version = latest["version"] if latest else 0
        if expected_version is not None and expected_version != version:
            raise ValueError(f"stale artifact version: expected {expected_version}, current {version}")
        if (latest and latest["content_sha256"] == digest
                and latest["cell_id"] == cell_id and latest["author"] == author
                and latest["source_group"] == source_group
                and latest["source_offsets"] == offsets_json):
            return _decode(latest)
        conn.execute("""INSERT INTO artifact_versions
            (task_id,attempt,cell_id,kind,artifact_id,version,author,payload,
             content_sha256,source_group,source_offsets,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, attempt, cell_id, kind, artifact_id, version + 1, author,
             encoded, digest, source_group, offsets_json, db._now()))
        conn.commit()
        return _decode(conn.execute("""SELECT * FROM artifact_versions
            WHERE task_id=? AND attempt=? AND kind=? AND artifact_id=? AND version=?""",
            (task_id, attempt, kind, artifact_id, version + 1)).fetchone())


def get_artifact(task_id: str, attempt: int, kind: str, artifact_id: str,
                 *, version: int | None = None) -> dict[str, Any] | None:
    _required_text(task_id, "task_id")
    _required_text(artifact_id, "artifact_id")
    if type(attempt) is not int or attempt < 1 or kind not in KINDS:
        raise ValueError("invalid artifact identity")
    conn = db._connect()
    _check_access(conn, task_id)
    sql = """SELECT * FROM artifact_versions WHERE task_id=? AND attempt=?
             AND kind=? AND artifact_id=?"""
    args: tuple[Any, ...] = (task_id, attempt, kind, artifact_id)
    if version is not None:
        if type(version) is not int or version < 1:
            raise ValueError("version must be positive")
        sql += " AND version=?"
        args += (version,)
    sql += " ORDER BY version DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    return _decode(row) if row else None


def list_artifacts(task_id: str, attempt: int, *, kind: str | None = None,
                   cell_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    _required_text(task_id, "task_id")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be positive")
    if kind is not None and kind not in KINDS:
        raise ValueError("unsupported artifact kind")
    if cell_id is not None:
        _required_text(cell_id, "cell_id")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    conn = db._connect()
    _check_access(conn, task_id)
    filters = ["task_id=?", "attempt=?"]
    args: list[Any] = [task_id, attempt]
    if kind:
        filters.append("kind=?")
        args.append(kind)
    if cell_id:
        filters.append("cell_id=?")
        args.append(cell_id)
    sql = "SELECT * FROM artifact_versions WHERE " + " AND ".join(filters)
    sql += " ORDER BY created_at DESC, artifact_id, version DESC LIMIT ?"
    return [_decode(row) for row in conn.execute(sql, (*args, limit)).fetchall()]


@dataclass(frozen=True)
class AgentContext:
    task_id: str
    attempt: int
    cell_id: str
    agent_id: str
    contract: dict[str, Any]
    cell: dict[str, Any]
    passages: tuple[dict[str, Any], ...]
    allowed_evidence_ids: tuple[str, ...]
    artifact_refs: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        result = {"task_id": self.task_id, "attempt": self.attempt,
                "cell_id": self.cell_id, "agent_id": self.agent_id,
                "contract": self.contract, "cell": self.cell,
                "passages": list(self.passages),
                "allowed_evidence_ids": list(self.allowed_evidence_ids),
                "artifact_refs": list(self.artifact_refs)}
        return json.loads(_canonical(result))


def build_agent_context(
    task_id: str, attempt: int, cell_id: str, agent_id: str,
    contract: dict[str, Any], cell: dict[str, Any],
    evidence_ids: Iterable[str], *, max_chars: int = 12000,
    max_tokens: int = 16000,
) -> AgentContext:
    """Select only named Evidence artifacts for one agent and one target cell.

    A caller must enumerate evidence IDs. No report, claim, plan, trace or other
    task history is implicitly loaded into the model-visible context.
    """
    _validate_scope(task_id, attempt, cell_id)
    _required_text(agent_id, "agent_id")
    if not isinstance(contract, dict) or not isinstance(cell, dict):
        raise TypeError("contract and cell must be dictionaries")
    # Snapshot caller state before selection so another agent cannot change it.
    contract = json.loads(_canonical(contract))
    cell = json.loads(_canonical(cell))
    if type(max_chars) is not int or type(max_tokens) is not int or max_chars < 512 or max_tokens < 512:
        raise ValueError("context budgets must be at least 512")
    ids = tuple(dict.fromkeys(evidence_ids))
    if len(ids) > 200 or any(not isinstance(eid, str) or not eid or len(eid) > 256 for eid in ids):
        raise ValueError("evidence_ids must contain at most 200 valid IDs")
    # Verify task access even for an empty whitelist.
    conn = db._connect()
    _check_access(conn, task_id)
    base = {"task_id": task_id, "attempt": attempt, "cell_id": cell_id,
            "agent_id": agent_id, "contract": contract, "cell": cell}
    base_json = _canonical(base)
    if len(base_json) + 256 > max_chars or len(base_json.encode("utf-8")) + 256 > max_tokens:
        raise ValueError("contract and cell exceed the agent context budget")
    rows = []
    for eid in ids:
        row = get_artifact(task_id, attempt, "Evidence", eid)
        if row:
            ev = dict(row["payload"])
            ev["evidence_id"] = eid
            ev.setdefault("source_group", row["source_group"])
            rows.append((row, ev))
    evidence = [ev for _, ev in rows]
    focus = [str(cell.get("dimension") or cell.get("field") or "")]
    selected = select_evidence(
        evidence, query=str(cell.get("query") or contract.get("query") or ""),
        brands=[str(cell["brand"])] if cell.get("brand") else [], focus=focus,
        max_chars=max_chars - len(base_json) - 256,
        max_tokens=max_tokens - len(base_json.encode("utf-8")) - 256,
    )
    by_id = {row["artifact_id"]: row for row, _ in rows}
    passages = list(selected.passages)
    while True:
        selected_ids = tuple(dict.fromkeys(p["evidence_id"] for p in passages))
        refs = tuple({"kind": "Evidence", "artifact_id": eid,
                      "version": by_id[eid]["version"],
                      "source_group": by_id[eid]["source_group"],
                      "source_offsets": by_id[eid]["source_offsets"]}
                     for eid in selected_ids)
        context = AgentContext(task_id, attempt, cell_id, agent_id, contract,
                               cell, tuple(passages), selected_ids, refs)
        encoded = _canonical(context.to_dict())
        if len(encoded) <= max_chars and len(encoded.encode("utf-8")) <= max_tokens:
            return context
        if not passages:
            raise ValueError("agent context metadata exceeds budget")
        passages.pop()
