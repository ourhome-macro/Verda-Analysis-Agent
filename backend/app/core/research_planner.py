"""Contract-bound, auditable action graph for research replanning."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from app.core.research_contract import cell_id, field_for

MAX_REPLAN_ROUNDS = 2
MAX_ACTIONS_PER_ROUND = 24
MAX_TOTAL_ACTIONS = 36
MAX_FETCH_PAGES_PER_ACTION = 4
ACTION_KINDS = frozenset({"search_alternate_source", "search_dated_official",
                          "refetch_reverify", "stop_ask_user"})
CONTRACT_KEYS = ("version", "brands", "dimensions", "as_of", "window_days",
                 "since", "freshness", "market", "user", "industry")


def _topic(value: object) -> str:
    if not isinstance(value, str) or "http://" in value or "https://" in value:
        return ""
    return re.sub(r"\s+", " ", value).strip()[:140]


def contract_fingerprint(contract: dict) -> str:
    frozen = {key: contract.get(key) for key in CONTRACT_KEYS}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def initial_topics(plan: dict, contract: dict) -> dict[str, str]:
    """Use dimension-specific LLM queries; map legacy angles when available."""
    dimensions = {d["key"]: d for d in contract["dimensions"]}
    topics: dict[str, str] = {}
    for row in plan.get("search_plan") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("dimension", "")).strip().lower()
        key = next((k for k, d in dimensions.items()
                    if name in (k.lower(), d["label"].lower())), field_for(name))
        value = _topic(row.get("query"))
        if key in dimensions and value:
            topics[key] = value
    angles = [_topic(x) for x in plan.get("angles") or []]
    angles = [x for x in angles if x]
    for key, dim in dimensions.items():
        words = [w for w in re.split(r"\s+", dim["query"] + " " + dim["label"]) if len(w) >= 2]
        matched = next((a for a in angles if any(w in a for w in words)), "")
        base = topics.get(key) or dim["query"]
        topics[key] = _topic(f"{base[:100]} {matched}" if matched and matched not in base else base)
    return topics


def _value(row: object, key: str, default=""):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _action_for(cell: dict, dim: dict, round_number: int, prior_actions: list[dict]) -> str:
    previous = {a.get("kind") for a in prior_actions if a.get("cell_id") == cell["cell_id"]}
    if cell["status"] == "covered":
        return "refetch_reverify"
    if cell["status"] == "background_only" and dim["source"] == "official":
        return ("search_dated_official" if "search_dated_official" not in previous
                else "search_alternate_source")
    if cell["status"] == "missing":
        return ("search_alternate_source" if "search_alternate_source" not in previous
                else "search_dated_official" if dim["source"] == "official"
                else "refetch_reverify")
    return "search_alternate_source" if round_number == 1 else "refetch_reverify"


def revise_plan(contract: dict, matrix: dict, cells: list[dict],
                initial: dict[str, str], review: dict, round_number: int, *,
                evidences: list | None = None, issues: list[dict] | None = None,
                previous_actions: list[dict] | None = None,
                fetch_budget: int = MAX_FETCH_PAGES_PER_ACTION) -> dict:
    """Produce a validated PlanDelta from observed cell and audit failures."""
    if not 1 <= round_number <= MAX_REPLAN_ROUNDS:
        raise ValueError("replan round exceeds hard limit")
    by_cell = {c["cell_id"]: c for c in matrix.get("cells", [])}
    by_dim = {d["key"]: d for d in contract["dimensions"]}
    issue_by_cell = {i.get("cell_id"): i for i in issues or [] if isinstance(i, dict)}
    as_of = datetime.fromisoformat(contract["as_of"])
    date_hint = f"{as_of.year}年{as_of.month}月" if contract.get("window_days") == 30 else str(as_of.year)
    actions, topics, reasons = [], {}, {}
    deferred = []
    prior_count = {cid: sum(a.get("cell_id") == cid for a in previous_actions or [])
                   for cid in by_cell}
    ordered_cells = sorted(cells, key=lambda row: prior_count.get(row.get("cell_id"), 0)
                           if isinstance(row, dict) else 0)
    remaining = MAX_TOTAL_ACTIONS - len(previous_actions or [])
    if remaining <= 0:
        raise ValueError("replan total action budget exhausted")
    round_limit = min(MAX_ACTIONS_PER_ROUND, remaining)
    for target in ordered_cells:
        if len(actions) >= round_limit:
            if (isinstance(target, dict) and target.get("cell_id") in by_cell
                    and (target.get("brand"), target.get("dimension")) ==
                    (by_cell[target["cell_id"]]["brand"], by_cell[target["cell_id"]]["dimension"])):
                deferred.append(target["cell_id"])
            continue
        cid = target.get("cell_id") if isinstance(target, dict) else None
        current = by_cell.get(cid)
        if not current or current["brand"] != target.get("brand") or current["dimension"] != target.get("dimension"):
            continue
        if any(a["cell_id"] == cid for a in actions):
            continue
        dim = by_dim[current["dimension"]]
        base = _topic(initial.get(dim["key"]) or dim["query"]) or dim["query"]
        kind = _action_for(current, dim, round_number, previous_actions or [])
        if kind == "search_dated_official":
            suffix, default_reason = f"{date_hint} 更新日志 发布公告 官方发布日期", "已核验事实缺研究窗口内的官方发布日期"
        elif kind == "search_alternate_source":
            suffix = (f"{date_hint} 独立社区 真实使用评价" if dim["source"] == "community"
                      else f"{date_hint} 官方原始资料 另一来源")
            default_reason = "现有来源未补足本单元，改查合规的其他来源"
        else:
            suffix, default_reason = "原始正文 适用范围 版本 计费口径", "重新抓取现有原始页并核验结论"
        if round_number == 2:
            suffix += " 版本 历史记录"
        reason = str(target.get("reason") or issue_by_cell.get(cid, {}).get("reason") or default_reason)[:400]
        topic = _topic(f"{base[:80]} {suffix}")
        eligible = [_value(e, "source_url") for e in evidences or []
                    if _value(e, "brand") == current["brand"]
                    and current["dimension"] in (_value(e, "research_dimensions", []) or [])
                    and _value(e, "fetch_kind") in ("body", "rendered")
                    and (dim["source"] == "mixed"
                         or dim["source"] == "official" and _value(e, "source_tier") == "official"
                         or dim["source"] == "community" and _value(e, "source_tier") in ("community", "media"))]
        action_budget = min(MAX_FETCH_PAGES_PER_ACTION, max(1, int(fetch_budget)))
        urls = list(dict.fromkeys(u for u in eligible if isinstance(u, str) and u.startswith(("https://", "http://"))))[:min(2, action_budget)]
        if kind == "refetch_reverify" and not urls:
            if current["status"] == "missing" or any(
                    _value(e, "brand") == current["brand"] for e in evidences or []):
                kind = "search_alternate_source"
                topic = _topic(f"{base[:80]} {date_hint} 独立来源 版本 历史记录")
                reason = str(target.get("reason") or "无可重新抓取的原始页；改查其他来源")[:400]
            else:
                kind = "stop_ask_user"
        action = {"action_id": f"r{round_number}:{cid}:{kind}", "kind": kind,
                  "cell_id": cid, "brand": current["brand"], "dimension": current["dimension"],
                  "failure": current["status"], "reason": reason,
                  "query": topic if kind.startswith("search_") else "",
                  "source_role": dim["source"], "refetch_urls": urls if kind == "refetch_reverify" else [],
                  "budget": {"fetch_pages": action_budget if kind != "stop_ask_user" else 0},
                  "depends_on": [], "stop_condition": "cell covered or action budget exhausted"}
        actions.append(action)
        topics[cid], reasons[cid] = topic, reason
    delta = {"version": round_number + 1, "round": round_number,
             "contract_version": contract["version"], "contract_fingerprint": contract_fingerprint(contract),
             "as_of": contract["as_of"], "reason": "依据证据矩阵和审计问题选择有界动作",
             "review_verdict": review.get("verdict", ""), "targets": [a["cell_id"] for a in actions],
             "deferred_targets": list(dict.fromkeys(deferred)),
             "search_topics": topics, "target_reasons": reasons, "actions": actions,
             "edges": [{"from": a["action_id"], "to": f"verify:{a['cell_id']}"}
                       for a in actions if a["kind"] != "stop_ask_user"],
             "budget": {"max_rounds": MAX_REPLAN_ROUNDS, "max_actions": MAX_ACTIONS_PER_ROUND,
                        "max_total_actions": MAX_TOTAL_ACTIONS,
                        "max_fetch_pages_per_action": MAX_FETCH_PAGES_PER_ACTION}}
    validate_plan_delta(delta, contract, matrix, evidences=evidences,
                        previous_actions=previous_actions)
    return delta


def validate_plan_delta(delta: dict, contract: dict, matrix: dict, *,
                        evidences: list | None = None,
                        previous_actions: list[dict] | None = None) -> None:
    """Reject changed scope, time, URL, action or budget before execution."""
    if delta.get("contract_version") != contract.get("version") or delta.get("as_of") != contract.get("as_of"):
        raise ValueError("PlanDelta changed research contract")
    if delta.get("contract_fingerprint") != contract_fingerprint(contract):
        raise ValueError("PlanDelta changed user scope or freshness")
    if matrix.get("contract") and contract_fingerprint(matrix["contract"]) != contract_fingerprint(contract):
        raise ValueError("PlanDelta matrix does not match research contract")
    round_number = delta.get("round")
    if type(round_number) is not int or not 1 <= round_number <= MAX_REPLAN_ROUNDS:
        raise ValueError("PlanDelta round exceeds hard limit")
    actions = delta.get("actions")
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS_PER_ROUND:
        raise ValueError("PlanDelta action budget exceeded")
    if len(actions) + len(previous_actions or []) > MAX_TOTAL_ACTIONS:
        raise ValueError("PlanDelta total action budget exceeded")
    if delta.get("budget") != {"max_rounds": MAX_REPLAN_ROUNDS,
                               "max_actions": MAX_ACTIONS_PER_ROUND,
                               "max_total_actions": MAX_TOTAL_ACTIONS,
                               "max_fetch_pages_per_action": MAX_FETCH_PAGES_PER_ACTION}:
        raise ValueError("PlanDelta changed hard budgets")
    allowed = {c["cell_id"]: c for c in matrix.get("cells", [])}
    dimensions = {d["key"]: d for d in contract["dimensions"]}
    contract_cells = {cell_id(b, d["key"]) for b in contract["brands"] for d in contract["dimensions"]}
    seen = set()
    for action in actions:
        if not isinstance(action, dict) or action.get("kind") not in ACTION_KINDS:
            raise ValueError("PlanDelta has unauthorized action")
        cid = action.get("cell_id")
        cell = allowed.get(cid)
        if cid in seen or cid not in contract_cells or not cell or (cell["brand"], cell["dimension"]) != (
                action.get("brand"), action.get("dimension")):
            raise ValueError("PlanDelta targets a non-contract cell")
        seen.add(cid)
        if (action.get("failure") != cell["status"]
                or action.get("source_role") != dimensions[cell["dimension"]]["source"]
                or action.get("action_id") != f"r{round_number}:{cid}:{action['kind']}"):
            raise ValueError("PlanDelta action does not match observed failure")
        if action["kind"] == "search_dated_official" and action["source_role"] != "official":
            raise ValueError("dated official search requires an official dimension")
        pages = action.get("budget", {}).get("fetch_pages")
        if type(pages) is not int or not 0 <= pages <= MAX_FETCH_PAGES_PER_ACTION:
            raise ValueError("PlanDelta fetch budget exceeded")
        if action["kind"] == "stop_ask_user":
            if pages:
                raise ValueError("stop action cannot spend fetch budget")
        elif not pages:
            raise ValueError("research action requires fetch budget")
        if action["kind"].startswith("search_") and not _topic(action.get("query")):
            raise ValueError("search action requires a safe query")
        urls = action.get("refetch_urls", [])
        evidence_urls = {_value(e, "source_url") for e in evidences or []
                         if _value(e, "brand") == action["brand"]
                         and action["dimension"] in (_value(e, "research_dimensions", []) or [])
                         and _value(e, "fetch_kind") in ("body", "rendered")
                         and (action["source_role"] == "mixed"
                              or action["source_role"] == "official" and _value(e, "source_tier") == "official"
                              or action["source_role"] == "community"
                              and _value(e, "source_tier") in ("community", "media"))}
        if (action["kind"] == "refetch_reverify"
                and (not isinstance(urls, list) or not urls or len(urls) > min(2, pages)
                     or any(u not in evidence_urls for u in urls))):
            raise ValueError("refetch URLs must come from existing evidence")
        if action["kind"] != "refetch_reverify" and urls:
            raise ValueError("non-refetch action cannot carry URLs")
        if action.get("depends_on"):
            raise ValueError("unrecognized dependency")
    if delta.get("targets") != [a["cell_id"] for a in actions]:
        raise ValueError("PlanDelta target list differs from actions")
    deferred = delta.get("deferred_targets")
    if (not isinstance(deferred, list) or len(deferred) != len(set(deferred))
            or any(cid not in allowed or cid in seen for cid in deferred)):
        raise ValueError("PlanDelta deferred targets are invalid")
    if delta.get("edges") != [{"from": a["action_id"], "to": f"verify:{a['cell_id']}"}
                             for a in actions if a["kind"] != "stop_ask_user"]:
        raise ValueError("PlanDelta graph edges differ from actions")
