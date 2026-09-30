"""Durable, contract-bound work queue for one research-run attempt.

Every mutation uses BEGIN IMMEDIATE.  SQLite, rather than a process-local lock,
decides the winner when workers in different processes claim the same item.
The lease token fences writes from a worker whose lease has expired.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from app.core import db
from app.core.research_contract import cell_id as contract_cell_id


KINDS = frozenset({
    "collect", "analyze", "verify", "audit", "rework", "stop_ask_user",
    "search_alternate_source", "search_dated_official", "refetch_reverify",
})
MAX_TASKS = 256
MAX_DEPTH = 3
MAX_CHILDREN = 16
MAX_BUDGET = 384
MAX_DEPENDENCIES = 8
MAX_RETRIES = 2
MAX_PAYLOAD_BYTES = 16_384
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "contract", "brand", "brands", "dimension", "dimensions", "cell_id",
    "as_of", "market", "window_days", "since", "freshness", "user",
    "industry", "query", "search_query", "url", "urls", "target_url",
    "fetch_url", "source_url", "source_urls", "refetch_urls", "scope",
    "action", "plan", "plan_delta",
})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _has_forbidden_payload_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(not isinstance(key, str) or key.lower() in _FORBIDDEN_PAYLOAD_KEYS
                   or _has_forbidden_payload_key(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_has_forbidden_payload_key(child) for child in value)
    return False


def _text(value: Any, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value.strip()


def _scope(task_id: str, attempt: int, cell_id: str | None = None) -> None:
    _text(task_id, "task_id")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if cell_id is not None:
        _text(cell_id, "cell_id")


def _access(conn, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        raise LookupError("task does not exist")
    if not db._owned(conn, "task", task_id):
        raise PermissionError("Task is not accessible")


def init() -> None:
    """Create the board independently of the main database migration."""
    with db._LOCK:
        c = db._connect()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS task_board_runs (
            task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
            contract_json TEXT NOT NULL, contract_sha256 TEXT NOT NULL,
            max_tasks INTEGER NOT NULL, max_depth INTEGER NOT NULL,
            max_budget INTEGER NOT NULL, used_budget INTEGER NOT NULL DEFAULT 0,
            claim_seq INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(task_id, attempt));
        CREATE TABLE IF NOT EXISTS task_board_cells (
            task_id TEXT NOT NULL, attempt INTEGER NOT NULL, cell_id TEXT NOT NULL,
            brand TEXT NOT NULL, dimension TEXT NOT NULL,
            last_claim_seq INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(task_id, attempt, cell_id));
        CREATE TABLE IF NOT EXISTS task_board_items (
            task_id TEXT NOT NULL, attempt INTEGER NOT NULL, work_id TEXT NOT NULL,
            cell_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, specification_sha256 TEXT NOT NULL,
            parent_id TEXT, dependencies TEXT NOT NULL, depth INTEGER NOT NULL,
            budget_cost INTEGER NOT NULL, priority INTEGER NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL, lease_token TEXT, lease_until REAL,
            worker_id TEXT, available_at REAL NOT NULL DEFAULT 0,
            result TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(task_id, attempt, work_id),
            UNIQUE(task_id, attempt, idempotency_key));
        CREATE INDEX IF NOT EXISTS idx_task_board_ready
            ON task_board_items(task_id, attempt, status, available_at, priority);
        """)
        c.commit()


def _transaction(fn):
    init()
    with db._LOCK:
        c = db._connect()
        c.execute("BEGIN IMMEDIATE")
        try:
            result = fn(c)
            c.commit()
            return result
        except BaseException:
            c.rollback()
            raise


def _decode(row) -> dict[str, Any]:
    item = dict(row)
    for name in ("payload", "dependencies", "result"):
        item[name] = json.loads(item[name]) if item[name] is not None else None
    return item


def _without_token(item: dict[str, Any]) -> dict[str, Any]:
    item.pop("lease_token", None)
    return item


def register_run(task_id: str, attempt: int, contract: dict, *,
                 max_tasks: int = MAX_TASKS, max_depth: int = MAX_DEPTH,
                 max_budget: int = MAX_BUDGET) -> dict[str, Any]:
    """Freeze allowed brand/dimension cells for this attempt; repeat is idempotent."""
    _scope(task_id, attempt)
    if not isinstance(contract, dict) or not isinstance(contract.get("brands"), list) or not isinstance(contract.get("dimensions"), list):
        raise ValueError("research contract requires brands and dimensions")
    brands = contract["brands"]
    dims = contract["dimensions"]
    if not brands or not dims or any(not isinstance(b, str) or not b.strip() for b in brands) or len(brands) != len(set(brands)):
        raise ValueError("research contract has invalid brands")
    keys = [d.get("key") for d in dims if isinstance(d, dict)]
    if len(keys) != len(dims) or any(not isinstance(k, str) or not k.strip() for k in keys) or len(keys) != len(set(keys)):
        raise ValueError("research contract has invalid dimensions")
    if not all(type(v) is int and 1 <= v <= cap for v, cap in
               ((max_tasks, MAX_TASKS), (max_depth, MAX_DEPTH), (max_budget, MAX_BUDGET))):
        raise ValueError("task board limits exceed hard caps")
    if len(brands) * len(dims) > max_tasks:
        raise ValueError("research contract cells exceed task cap")
    frozen = _json(contract)
    fingerprint = hashlib.sha256(frozen.encode()).hexdigest()

    def write(c):
        _access(c, task_id)
        old = c.execute("SELECT * FROM task_board_runs WHERE task_id=? AND attempt=?",
                        (task_id, attempt)).fetchone()
        if old:
            if (old["contract_sha256"], old["max_tasks"], old["max_depth"], old["max_budget"]) != (
                    fingerprint, max_tasks, max_depth, max_budget):
                raise ValueError("research contract or board limits changed within attempt")
            return dict(old)
        c.execute("INSERT INTO task_board_runs(task_id,attempt,contract_json,contract_sha256,max_tasks,max_depth,max_budget) VALUES(?,?,?,?,?,?,?)",
                  (task_id, attempt, frozen, fingerprint, max_tasks, max_depth, max_budget))
        c.executemany("INSERT INTO task_board_cells(task_id,attempt,cell_id,brand,dimension) VALUES(?,?,?,?,?)",
                      [(task_id, attempt, contract_cell_id(brand, key), brand, key)
                       for brand in brands for key in keys])
        return dict(c.execute("SELECT * FROM task_board_runs WHERE task_id=? AND attempt=?",
                              (task_id, attempt)).fetchone())
    return _transaction(write)


def create_work(task_id: str, attempt: int, cell_id: str, kind: str, *,
                idempotency_key: str, parent_id: str | None = None,
                parent_token: str | None = None, depends_on=(),
                payload: dict | None = None, priority: int = 0,
                max_retries: int = MAX_RETRIES, budget_cost: int = 1) -> dict[str, Any]:
    """Enqueue a root or leased-agent child in a known contract cell.

    Payload holds references and reasons only. Search terms, URLs and scope
    changes must come from the separately validated research PlanDelta.
    """
    _scope(task_id, attempt, cell_id)
    _text(idempotency_key, "idempotency_key")
    if kind not in KINDS:
        raise ValueError("work kind is not authorized")
    if type(priority) is not int or not -10 <= priority <= 10:
        raise ValueError("priority must be between -10 and 10")
    if type(max_retries) is not int or not 0 <= max_retries <= MAX_RETRIES:
        raise ValueError("retry limit exceeds hard cap")
    if type(budget_cost) is not int or not 1 <= budget_cost <= 16:
        raise ValueError("budget cost must be between 1 and 16")
    if payload is not None and (not isinstance(payload, dict) or
                                _has_forbidden_payload_key(payload)):
        raise ValueError("payload may not carry a new research scope, query, action or URL")
    payload = payload or {}
    encoded_payload = _json(payload)
    if len(encoded_payload.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("work payload exceeds 16 KiB")
    if depends_on is None or isinstance(depends_on, (str, bytes)):
        raise ValueError("invalid dependencies")
    deps = list(depends_on)
    if (len(deps) > MAX_DEPENDENCIES or any(not isinstance(d, str) or not d for d in deps)
            or len(deps) != len(set(deps))):
        raise ValueError("invalid dependencies")
    if parent_id is None and parent_token is not None or parent_id is not None and not parent_token:
        raise ValueError("child creation requires the current parent lease token")
    specification = _json({"cell_id": cell_id, "kind": kind, "payload": payload,
                           "parent_id": parent_id, "dependencies": deps,
                           "priority": priority, "max_retries": max_retries,
                           "budget_cost": budget_cost})
    spec_hash = hashlib.sha256(specification.encode()).hexdigest()
    work_id = "work_" + hashlib.sha256(f"{task_id}\0{attempt}\0{idempotency_key}".encode()).hexdigest()[:24]

    def write(c):
        _access(c, task_id)
        run = c.execute("SELECT * FROM task_board_runs WHERE task_id=? AND attempt=?",
                        (task_id, attempt)).fetchone()
        if not run:
            raise LookupError("task board attempt is not registered")
        if not c.execute("SELECT 1 FROM task_board_cells WHERE task_id=? AND attempt=? AND cell_id=?",
                         (task_id, attempt, cell_id)).fetchone():
            raise ValueError("work targets a cell outside the research contract")
        depth = 0
        if parent_id:
            parent = c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                               (task_id, attempt, parent_id)).fetchone()
            if not parent or parent["status"] != "claimed" or parent["lease_token"] != parent_token or parent["lease_until"] <= time.time():
                raise PermissionError("parent lease is absent or expired")
            if parent["cell_id"] != cell_id:
                raise ValueError("child must remain in parent contract cell")
            depth = parent["depth"] + 1
        old = c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND idempotency_key=?",
                        (task_id, attempt, idempotency_key)).fetchone()
        if old:
            if old["specification_sha256"] != spec_hash:
                raise ValueError("idempotency key was reused for different work")
            return _without_token(_decode(old))
        if parent_id:
            count = c.execute("SELECT count(*) FROM task_board_items WHERE task_id=? AND attempt=? AND parent_id=?",
                              (task_id, attempt, parent_id)).fetchone()[0]
            if count >= MAX_CHILDREN:
                raise ValueError("parent child-task limit exceeded")
        if depth > run["max_depth"]:
            raise ValueError("task depth limit exceeded")
        for dep in deps:
            if not c.execute("SELECT 1 FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                             (task_id, attempt, dep)).fetchone():
                raise ValueError("dependency must be an existing work item in this attempt")
        if c.execute("SELECT count(*) FROM task_board_items WHERE task_id=? AND attempt=?",
                     (task_id, attempt)).fetchone()[0] >= run["max_tasks"]:
            raise ValueError("task count limit exceeded")
        if run["used_budget"] + budget_cost > run["max_budget"]:
            raise ValueError("task budget exceeded")
        now = time.time()
        c.execute("INSERT INTO task_board_items(task_id,attempt,work_id,cell_id,kind,payload,idempotency_key,specification_sha256,parent_id,dependencies,depth,budget_cost,priority,status,max_retries,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (task_id, attempt, work_id, cell_id, kind, encoded_payload,
                   idempotency_key, spec_hash, parent_id, _json(deps), depth,
                   budget_cost, priority, "pending", max_retries, now, now))
        c.execute("UPDATE task_board_runs SET used_budget=used_budget+? WHERE task_id=? AND attempt=?",
                  (budget_cost, task_id, attempt))
        return _without_token(_decode(c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                                                (task_id, attempt, work_id)).fetchone()))
    return _transaction(write)


def _block_dependents(c, task_id: str, attempt: int, now: float) -> None:
    """Propagate terminal prerequisite/parent failures without executing descendants."""
    while True:
        failed = {r[0] for r in c.execute(
            "SELECT work_id FROM task_board_items WHERE task_id=? AND attempt=? AND status IN ('failed','blocked')",
            (task_id, attempt))}
        changed = 0
        for row in c.execute("SELECT work_id,parent_id,dependencies FROM task_board_items WHERE task_id=? AND attempt=? AND status IN ('pending','claimed')",
                             (task_id, attempt)):
            if row["parent_id"] in failed or failed.intersection(json.loads(row["dependencies"])):
                changed += c.execute("UPDATE task_board_items SET status='blocked',error=?,lease_token=NULL,lease_until=NULL,worker_id=NULL,updated_at=? WHERE task_id=? AND attempt=? AND work_id=? AND status IN ('pending','claimed')",
                                     ("parent or dependency failed", now, task_id, attempt, row["work_id"])).rowcount
        if not changed:
            return


def claim_work(task_id: str, attempt: int, worker_id: str, *,
               kind: str | None = None, cell_id: str | None = None,
               lease_seconds: float = 45) -> dict[str, Any] | None:
    """Fairly claim one ready item; expired leases are recovered atomically."""
    _scope(task_id, attempt, cell_id)
    _text(worker_id, "worker_id")
    if kind is not None and kind not in KINDS:
        raise ValueError("work kind is not authorized")
    if not isinstance(lease_seconds, (float, int)) or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")

    def write(c):
        _access(c, task_id)
        now = time.time()
        c.execute("UPDATE task_board_items SET status='pending',lease_token=NULL,lease_until=NULL,worker_id=NULL,updated_at=? WHERE task_id=? AND attempt=? AND status='claimed' AND lease_until<=? AND attempts<=max_retries",
                  (now, task_id, attempt, now))
        c.execute("UPDATE task_board_items SET status='failed',lease_token=NULL,lease_until=NULL,worker_id=NULL,error='lease expired; retry limit reached',updated_at=? WHERE task_id=? AND attempt=? AND status='claimed' AND lease_until<=? AND attempts>max_retries",
                  (now, task_id, attempt, now))
        _block_dependents(c, task_id, attempt, now)
        sql = """SELECT i.* FROM task_board_items i
            JOIN task_board_cells f ON f.task_id=i.task_id AND f.attempt=i.attempt AND f.cell_id=i.cell_id
            WHERE i.task_id=? AND i.attempt=? AND i.status='pending' AND i.available_at<=?"""
        args: tuple = (task_id, attempt, now)
        if kind is not None:
            sql += " AND i.kind=?"
            args += (kind,)
        if cell_id is not None:
            sql += " AND i.cell_id=?"
            args += (cell_id,)
        sql += " ORDER BY f.last_claim_seq ASC, i.priority DESC, i.created_at ASC, i.work_id ASC"
        rows = c.execute(sql, args).fetchall()
        for row in rows:
            deps = json.loads(row["dependencies"])
            if deps:
                done = {r[0] for r in c.execute(
                    "SELECT work_id FROM task_board_items WHERE task_id=? AND attempt=? AND status='done'",
                    (task_id, attempt))}
                if not set(deps).issubset(done):
                    continue
            token = uuid.uuid4().hex
            c.execute("UPDATE task_board_items SET status='claimed',attempts=attempts+1,worker_id=?,lease_token=?,lease_until=?,updated_at=? WHERE task_id=? AND attempt=? AND work_id=? AND status='pending'",
                      (worker_id, token, now + lease_seconds, now, task_id, attempt, row["work_id"]))
            c.execute("UPDATE task_board_runs SET claim_seq=claim_seq+1 WHERE task_id=? AND attempt=?",
                      (task_id, attempt))
            c.execute("UPDATE task_board_cells SET last_claim_seq=(SELECT claim_seq FROM task_board_runs WHERE task_id=? AND attempt=?) WHERE task_id=? AND attempt=? AND cell_id=?",
                      (task_id, attempt, task_id, attempt, row["cell_id"]))
            return _decode(c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                                     (task_id, attempt, row["work_id"])).fetchone())
        return None
    return _transaction(write)


def _lease(c, task_id: str, attempt: int, work_id: str, lease_token: str):
    row = c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                    (task_id, attempt, work_id)).fetchone()
    if not row:
        raise LookupError("work item does not exist")
    if row["status"] != "claimed" or row["lease_token"] != lease_token or row["lease_until"] <= time.time():
        raise PermissionError("work lease is absent or expired")
    return row


def heartbeat_work(task_id: str, attempt: int, work_id: str, lease_token: str, *,
                   lease_seconds: float = 45) -> dict[str, Any]:
    _scope(task_id, attempt)
    if not isinstance(lease_seconds, (float, int)) or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    def write(c):
        _access(c, task_id)
        _lease(c, task_id, attempt, work_id, lease_token)
        now = time.time()
        c.execute("UPDATE task_board_items SET lease_until=?,updated_at=? WHERE task_id=? AND attempt=? AND work_id=?",
                  (now + lease_seconds, now, task_id, attempt, work_id))
        return _decode(c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                                 (task_id, attempt, work_id)).fetchone())
    return _transaction(write)


def complete_work(task_id: str, attempt: int, work_id: str, lease_token: str,
                  result: dict | None = None) -> dict[str, Any]:
    _scope(task_id, attempt)
    if result is not None and not isinstance(result, dict):
        raise ValueError("result must be a dictionary")
    encoded = _json(result or {})
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("work result exceeds 16 KiB")
    def write(c):
        _access(c, task_id)
        _lease(c, task_id, attempt, work_id, lease_token)
        c.execute("UPDATE task_board_items SET status='done',result=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE task_id=? AND attempt=? AND work_id=?",
                  (encoded, time.time(), task_id, attempt, work_id))
        return _decode(c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                                 (task_id, attempt, work_id)).fetchone())
    return _transaction(write)


def fail_work(task_id: str, attempt: int, work_id: str, lease_token: str,
              error: str, *, retry_delay_seconds: float = 0) -> dict[str, Any]:
    _scope(task_id, attempt)
    _text(error, "error", 1000)
    if not isinstance(retry_delay_seconds, (float, int)) or not 0 <= retry_delay_seconds <= 3600:
        raise ValueError("retry delay must be between 0 and 3600 seconds")
    def write(c):
        _access(c, task_id)
        row = _lease(c, task_id, attempt, work_id, lease_token)
        now = time.time()
        status = "pending" if row["attempts"] <= row["max_retries"] else "failed"
        c.execute("UPDATE task_board_items SET status=?,error=?,lease_token=NULL,lease_until=NULL,worker_id=NULL,available_at=?,updated_at=? WHERE task_id=? AND attempt=? AND work_id=?",
                  (status, error, now + retry_delay_seconds, now, task_id, attempt, work_id))
        if status == "failed":
            _block_dependents(c, task_id, attempt, now)
        return _decode(c.execute("SELECT * FROM task_board_items WHERE task_id=? AND attempt=? AND work_id=?",
                                 (task_id, attempt, work_id)).fetchone())
    return _transaction(write)


def list_work(task_id: str, attempt: int, *, cell_id: str | None = None) -> list[dict[str, Any]]:
    _scope(task_id, attempt, cell_id)
    init()
    c = db._connect()
    _access(c, task_id)
    sql = "SELECT * FROM task_board_items WHERE task_id=? AND attempt=?"
    args: tuple = (task_id, attempt)
    if cell_id is not None:
        sql += " AND cell_id=?"
        args += (cell_id,)
    return [_without_token(_decode(r)) for r in c.execute(sql + " ORDER BY created_at,work_id", args)]


def summary(task_id: str, attempt: int) -> dict[str, int]:
    _scope(task_id, attempt)
    init()
    c = db._connect()
    _access(c, task_id)
    result = {state: 0 for state in ("pending", "claimed", "done", "failed", "blocked")}
    result.update({r["status"]: r["n"] for r in c.execute(
        "SELECT status,count(*) AS n FROM task_board_items WHERE task_id=? AND attempt=? GROUP BY status",
        (task_id, attempt))})
    return result
