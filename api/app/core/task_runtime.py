"""SQLite job leases + persistent events; SSE observes an independent worker.

Single-host execution: leases bound concurrent workers even across processes.
On process loss, mark interrupted; an explicit retry starts a new attempt from
the beginning. This is not per-node checkpoint resume. Visitor keys stay in RAM.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress

from app.core import db, trace, llm, performance
from app.core.config import use_request_settings

_workers = {}
LEASE_SECONDS = 45


def init():
    with db._LOCK:
        c = db._connect()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS research_runs (
            task_id TEXT PRIMARY KEY, owner_id TEXT, attempt INTEGER NOT NULL,
            status TEXT NOT NULL, lease_until REAL, updated_at REAL);
        CREATE TABLE IF NOT EXISTS research_events (
            task_id TEXT NOT NULL, seq INTEGER NOT NULL, attempt INTEGER NOT NULL,
            event_type TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(task_id,seq));
        """)
        c.commit()


def state(task_id):
    init()
    with db._LOCK:
        c = db._connect()
        row = c.execute("SELECT * FROM research_runs WHERE task_id=?", (task_id,)).fetchone()
        if row and row["status"] == "running" and row["lease_until"] < time.time():
            c.execute("UPDATE research_runs SET status='interrupted' WHERE task_id=? AND lease_until<?",
                      (task_id, time.time()))
            c.commit()
            row = c.execute("SELECT * FROM research_runs WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def active_count():
    init()
    return db._connect().execute("SELECT count(*) FROM research_runs WHERE status='running' AND lease_until>?",
                                 (time.time(),)).fetchone()[0]


def collection_checkpoint(task_id):
    """Resume a recent explicit retry from already emitted source documents.

    Older than one hour -> fresh collection; analysis and audit always run again.
    """
    import datetime
    from dataclasses import fields
    from app.core.models import Evidence
    task = db.get_task(task_id)
    if not task or not task.get("created_at"):
        return []
    created = datetime.datetime.fromisoformat(task["created_at"]).timestamp()
    if time.time() - created > 3600:
        return []
    run = state(task_id)
    if not run or run["attempt"] <= 1:
        return []
    keys = {f.name for f in fields(Evidence)}
    pool = {}
    for row in db._connect().execute("SELECT data FROM research_events WHERE task_id=? AND attempt<? AND event_type='evidence' ORDER BY seq",
                                    (task_id, run["attempt"])):
        data = json.loads(row["data"])
        try:
            ev = Evidence(**{k: v for k, v in data.items() if k in keys})
            pool[(ev.brand, ev.source_url, tuple(ev.research_dimensions))] = ev
        except (TypeError, ValueError):
            continue
    return list(pool.values())


def append_event(task_id, attempt, event):
    with db._LOCK:
        c = db._connect()
        c.execute("INSERT INTO research_events(task_id,seq,attempt,event_type,data) "
                  "SELECT ?,COALESCE(MAX(seq),0)+1,?,?,? FROM research_events WHERE task_id=?",
                  (task_id, attempt, event["type"], json.dumps(event["data"], ensure_ascii=False), task_id))
        c.commit()


def events(task_id, after=0):
    return [{"seq": r["seq"], "type": r["event_type"], "data": json.loads(r["data"])} for r in
            db._connect().execute("SELECT * FROM research_events WHERE task_id=? AND seq>? AND attempt="
                                  "(SELECT attempt FROM research_runs WHERE task_id=research_events.task_id) ORDER BY seq LIMIT 100",
                                  (task_id, after)).fetchall()]


async def start(task_id, visitor_id, settings, pipeline, *, sub_id="", retry=False):
    init()
    with db._LOCK:
        c = db._connect()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT * FROM research_runs WHERE task_id=?", (task_id,)).fetchone()
            if row and ((row["status"] == "done" and not retry) or (row["status"] == "running" and row["lease_until"] > time.time())):
                c.rollback()
                return
            if row and not retry:
                c.rollback()
                return
            active = c.execute("SELECT owner_id FROM research_runs WHERE status='running' AND lease_until>?",
                               (time.time(),)).fetchall()
            if len(active) >= 2 or (visitor_id and any(r["owner_id"] == visitor_id for r in active)):
                raise RuntimeError("当前调研任务较多，请稍后重试")
            attempt = (row["attempt"] + 1) if row else 1
            c.execute("INSERT OR REPLACE INTO research_runs VALUES(?,?,?,?,?,?)",
                      (task_id, visitor_id, attempt, "running", time.time() + LEASE_SECONDS, time.time()))
            c.execute("UPDATE tasks SET status='running' WHERE task_id=?", (task_id,))
            c.commit()
        except Exception:
            c.rollback()
            raise
    _workers[task_id] = asyncio.create_task(_run(task_id, attempt, visitor_id, settings, pipeline, sub_id))


async def _run(task_id, attempt, visitor_id, settings, pipeline, sub_id):
    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            with db._LOCK:
                c = db._connect()
                c.execute("UPDATE research_runs SET lease_until=? WHERE task_id=? AND attempt=? AND status='running'",
                          (time.time() + LEASE_SECONDS, task_id, attempt))
                c.commit()
    beat = asyncio.create_task(heartbeat())
    status = "failed"
    try:
        with db.use_visitor(visitor_id), use_request_settings(settings), llm.use_request_client(), performance.use_task(task_id):
            async for event in pipeline(task_id, sub_id=sub_id):
                if event["type"] == "node_update":
                    performance.on_node_update(task_id, event["data"])
                append_event(task_id, attempt, event)
                if event["type"] == "done":
                    status = "done"
            if status != "done":
                append_event(task_id, attempt, {"type": "error", "data": {"message": "任务未完成，可显式重试"}})
    except asyncio.CancelledError:
        status = "interrupted"
        append_event(task_id, attempt, {"type": "error", "data": {"message": "服务停止，任务已中断；重试将从头开始"}})
        raise
    except Exception as exc:
        append_event(task_id, attempt, {"type": "error", "data": {"message": "任务执行失败：" + type(exc).__name__}})
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError):
            await beat
        try:
            db.save_attempt_performance(task_id, attempt, status,
                                        performance.snapshot(task_id))
        except Exception:
            pass  # Metrics must never prevent task state cleanup.
        with db._LOCK:
            c = db._connect()
            c.execute("UPDATE research_runs SET status=?,updated_at=? WHERE task_id=? AND attempt=?",
                      (status, time.time(), task_id, attempt))
            if status != "done":
                c.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))
            c.commit()
        trace.cleanup(task_id)
        performance.cleanup(task_id)
        _workers.pop(task_id, None)


async def observe(task_id, request, *, after=0):
    cursor, keepalive = after, time.monotonic()
    while not await request.is_disconnected():
        batch = events(task_id, cursor)
        for event in batch:
            cursor = event["seq"]
            yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
            if event["type"] in ("done", "error"):
                return
        status = state(task_id)
        if not batch and status and status["status"] in ("done", "failed", "interrupted"):
            if status["status"] == "done":
                task = db.get_task(task_id) or {}
                data = {"reportId": task.get("report_id")}
                yield f"event: done\ndata: {json.dumps(data)}\n\n"
            else:
                yield 'event: error\ndata: {"message":"任务已中断，可显式重试；已有事件可回放"}\n\n'
            return
        if time.monotonic() - keepalive > 10:
            yield ": keepalive\n\n"
            keepalive = time.monotonic()
        await asyncio.sleep(0.2)
