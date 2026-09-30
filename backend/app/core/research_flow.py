"""Bounded audit to replan routing for the research pipeline.

The graph owns the decision to create a PlanDelta. Task Board owns execution
and ArtifactStore owns durable outputs; neither is duplicated in graph state.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.research_planner import MAX_REPLAN_ROUNDS, revise_plan


class AuditFlowState(TypedDict, total=False):
    contract: dict
    matrix: dict
    targets: list[dict]
    search_topics: dict[str, str]
    review: dict
    round_number: int
    max_rounds: int
    evidences: list
    issues: list[dict]
    previous_actions: list[dict]
    fetch_budget: int
    route: str
    revision: dict | None


def _audit_route(state: AuditFlowState) -> dict[str, Any]:
    round_number = state["round_number"]
    max_rounds = state["max_rounds"]
    if type(round_number) is not int or type(max_rounds) is not int or not 0 <= max_rounds <= MAX_REPLAN_ROUNDS:
        raise ValueError("invalid rework budget")
    if round_number < 1 or round_number > MAX_REPLAN_ROUNDS:
        raise ValueError("invalid replan round")
    return {"route": "replan" if state["targets"] and round_number <= max_rounds else "write"}


def _make_revision(state: AuditFlowState) -> dict[str, Any]:
    revision = revise_plan(
        state["contract"], state["matrix"], state["targets"],
        state["search_topics"], state["review"], state["round_number"],
        evidences=state.get("evidences"), issues=state.get("issues"),
        previous_actions=state.get("previous_actions"),
        fetch_budget=state.get("fetch_budget", 4),
    )
    return {"revision": revision}


_builder = StateGraph(AuditFlowState)
_builder.add_node("audit_route", _audit_route)
_builder.add_node("make_revision", _make_revision)
_builder.add_edge(START, "audit_route")
_builder.add_conditional_edges("audit_route", lambda state: state["route"],
                               {"replan": "make_revision", "write": END})
_builder.add_edge("make_revision", END)
AUDIT_FLOW = _builder.compile()


def _evidence_refs(evidences: list | None) -> list[dict]:
    """Keep source metadata in graph state; full text stays in the artifact store."""
    keys = ("brand", "research_dimensions", "source_url", "fetch_kind", "source_tier")
    return [{key: item.get(key) if isinstance(item, dict) else getattr(item, key, None)
             for key in keys} for item in evidences or []]


def next_replan(contract: dict, matrix: dict, targets: list[dict],
                search_topics: dict[str, str], review: dict, round_number: int,
                max_rounds: int, *, evidences: list | None = None,
                issues: list[dict] | None = None,
                previous_actions: list[dict] | None = None,
                fetch_budget: int = 4) -> dict | None:
    """Return a validated PlanDelta when the audit graph permits another round."""
    if type(max_rounds) is not int or max_rounds < 0:
        raise ValueError("invalid rework budget")
    max_rounds = min(max_rounds, MAX_REPLAN_ROUNDS)
    result = AUDIT_FLOW.invoke({
        "contract": contract, "matrix": matrix, "targets": targets,
        "search_topics": search_topics, "review": review,
        "round_number": round_number, "max_rounds": max_rounds,
        "evidences": _evidence_refs(evidences), "issues": issues or [],
        "previous_actions": previous_actions or [], "fetch_budget": fetch_budget,
    })
    return result.get("revision")
