"""Execute one matrix cell through a scoped specialist and durable work lease."""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from app.core import artifact_store, task_board
from app.core.agent_tools import (AgentToolRegistry, AgentToolSpec,
                                  DelegationBudget)
from app.core.models import Evidence


class AnalyzeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cell_id: str
    brand: str
    dimension: str
    evidence_ids: list[str]


class AnalyzeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[dict[str, Any]]
    evidence_selection: list[dict[str, Any]]
    analysis_error: str | None = None


@contextmanager
def renew_work_lease(task_id: str, attempt: int, work: dict):
    """Keep a synchronous LLM call claimed while its worker is alive."""
    done = threading.Event()
    context = contextvars.copy_context()

    def loop():
        while not done.wait(10):
            try:
                task_board.heartbeat_work(task_id, attempt, work["work_id"],
                                          work["lease_token"], lease_seconds=45)
            except Exception:
                return  # Completion is fenced and will fail if the lease was lost.

    worker = threading.Thread(target=lambda: context.run(loop), daemon=True,
                              name="research-work-lease")
    worker.start()
    try:
        yield
    finally:
        done.set()
        worker.join(timeout=1)


def analyze_cell(
    task_id: str, attempt: int, agent_id: str, contract: dict, cell: dict,
    evidence_ids: list[str], round_number: int,
    analyzer: Callable[[str, str, list[Evidence]], dict],
) -> dict:
    """Claim a cell, give its analyst only allowlisted evidence, persist result."""
    cid = cell["cell_id"]
    work = task_board.create_work(
        task_id, attempt, cid, "analyze",
        idempotency_key=f"analyze:{round_number}:{cid}",
        payload={"plan_version": round_number + 1}, max_retries=0)
    claimed = task_board.claim_work(task_id, attempt,
                                   f"{agent_id}:{threading.get_ident()}",
                                   kind="analyze", cell_id=cid, lease_seconds=45)
    if not claimed or claimed["work_id"] != work["work_id"]:
        raise RuntimeError("analysis work was not claimable")

    try:
        local = artifact_store.build_agent_context(
            task_id, attempt, cid, agent_id, contract, cell, evidence_ids)
        registry = AgentToolRegistry()

        def handler(request: AnalyzeInput, scoped) -> dict:
            scoped.require("evidence_read")
            selected = []
            for ref in scoped.artifact_refs:
                row = artifact_store.get_artifact(
                    task_id, attempt, "Evidence", ref["artifact_id"],
                    version=ref["version"])
                if row is None:
                    raise LookupError("evidence artifact disappeared")
                selected.append(Evidence(**row["payload"]))
            return analyzer(request.brand, request.dimension, selected)

        registry.register(AgentToolSpec(
            name="analyze_cell", role="analyst", input_model=AnalyzeInput,
            output_model=AnalyzeOutput, handler=handler,
            capabilities=frozenset({"evidence_read", "claim_create"})))
        with renew_work_lease(task_id, attempt, claimed):
            result = registry.invoke(
                "analyze_cell",
                {"cell_id": cid, "brand": cell["brand"],
                 "dimension": cell["dimension"],
                 "evidence_ids": list(local.allowed_evidence_ids)},
                context=local,
                budget=DelegationBudget(max_calls=1, max_elapsed_ms=600_000))
        output = result.output
        if output.get("analysis_error"):
            task_board.fail_work(task_id, attempt, claimed["work_id"],
                                 claimed["lease_token"], "analysis_error")
            return output
        for claim in output["claims"]:
            artifact_store.publish_artifact(
                task_id, attempt, cid, "Claim", claim["claim_id"], claim,
                author=agent_id)
        task_board.complete_work(
            task_id, attempt, claimed["work_id"], claimed["lease_token"],
            result={"claim_ids": [c["claim_id"] for c in output["claims"]],
                    "delegation_span_id": result.span_id})
        return output
    except Exception as exc:
        try:
            task_board.fail_work(task_id, attempt, claimed["work_id"],
                                 claimed["lease_token"], type(exc).__name__)
        except (LookupError, PermissionError):
            pass
        raise
