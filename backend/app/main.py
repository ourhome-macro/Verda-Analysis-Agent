"""青野 Verda 后端入口（FastAPI）。

挂载：48 专家 API + 任务创建/澄清 + SSE 思维流 + 报告/历史 + 仪表盘统计
+ 全局证据溯源库 + 竞品监控订阅 + 专家工作量看板 + 健康/验证接口。
可配置 LLM Provider + 真实搜索（博查 Bocha）+ 真实抓取 + SQLite 持久化。
"""
from __future__ import annotations

import json
import hashlib
import re
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import db
from app.core import task_runtime
from app.core.config import _ENV_FILE, clear_settings_cache, get_settings, use_request_settings
from app.core.client_credentials import browser_settings
from app.core.local_settings import local_edit_allowed, save_local_credentials
from app.core import llm as llm_core
from app.core.llm import LLMNotConfigured, chat
from app.core.orchestrator import create_task, run_pipeline, submit_clarify, refine_section
from app.core.search import search
from app.data import expert_by_id, load_experts

_config_lock = threading.Lock()
_active_streams = 0
_active_task_ids: set[str] = set()
_active_visitors: set[str] = set()
_MAX_ACTIVE_STREAMS = 2

app = FastAPI(title="青野 Verda API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_VISITOR_TOKEN = re.compile(r"^[0-9a-f]{64}$")


@app.middleware("http")
async def bind_visitor(request: Request, call_next):
    visitor_id = None
    if request.method != "OPTIONS" and request.url.path.startswith("/api/") and get_settings().require_client_api_keys:
        token = request.headers.get("x-verda-visitor", "")
        if not _VISITOR_TOKEN.fullmatch(token):
            return JSONResponse(status_code=400, content={"detail": "浏览器访问凭据缺失，请刷新页面"})
        visitor_id = hashlib.sha256(token.encode("ascii")).hexdigest()
    request.state.visitor_id = visitor_id
    with db.use_visitor(visitor_id):
        response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


# ── 基础 / 健康 ─────────────────────────────────────────
@app.get("/")
def root():
    settings = get_settings()
    return {
        "name": "青野 Verda API",
        "version": "2.0.0",
        "slogan": "让每个结论都有出处，让每次调研都活着。",
        "llm_configured": settings.llm_configured and not settings.require_client_api_keys,
        "experts": len(load_experts()),
    }


@app.get("/health")
def health():
    settings = get_settings()
    return {"status": "ok", "provider": settings.provider_config.name,
            "llm_configured": settings.llm_configured and not settings.require_client_api_keys,
            "search_configured": bool(settings.bocha_api_key) and not settings.require_client_api_keys,
            "client_keys_required": settings.require_client_api_keys,
            "public_access_ready": settings.require_client_api_keys}


def _request_settings(request: Request):
    try:
        return browser_settings(request.headers, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/llm/ping")
def llm_ping(request: Request):
    with use_request_settings(_request_settings(request)):
        with llm_core.use_request_client():
            return _llm_ping()


def _llm_ping():
    try:
        settings = get_settings()
        reply = chat(
            [
                {"role": "system", "content": "你只回复一个词。"},
                {"role": "user", "content": "请回复：可用"},
            ],
            max_tokens=200,
        )
        return {"ok": True, "provider": settings.provider_config.name,
                "model": settings.provider_config.default_model, "reply": reply.strip()}
    except LLMNotConfigured as e:
        return {"ok": False, "reason": "not_configured", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "error", "message": str(e)}


def _config_payload(settings, editable: bool):
    provider = settings.provider_config
    return {"provider": provider.name, "model": provider.default_model,
            "core_model": provider.core_model, "aux_model": provider.aux_model,
            "fast_model": provider.fast_model,
            "configured": settings.llm_configured and not settings.require_client_api_keys,
            "search_configured": bool(settings.bocha_api_key) and not settings.require_client_api_keys,
            "editable": editable, "client_keys_required": settings.require_client_api_keys}


@app.get("/api/llm/config")
def llm_config(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    host = request.client.host if request.client else ""
    editable = local_edit_allowed(settings, host, request.headers.get("origin", ""))
    return _config_payload(settings, editable)


class UpdateLLMConfigBody(BaseModel):
    provider: str
    api_key: str = ""
    bocha_api_key: str = ""
    base_url: str = ""
    model: str = ""


@app.put("/api/llm/config")
def update_llm_config(body: UpdateLLMConfigBody, request: Request, response: Response):
    global _active_streams
    settings = get_settings()
    host = request.client.host if request.client else ""
    origin = request.headers.get("origin", "")
    if not local_edit_allowed(settings, host, origin):
        raise HTTPException(status_code=403, detail="仅本机启用的配置页面可以修改 API Key")
    with _config_lock:
        if task_runtime.active_count():
            raise HTTPException(status_code=409, detail="调研正在运行，请等待任务结束后再修改模型配置")
        try:
            save_local_credentials(
                provider=body.provider, api_key=body.api_key,
                bocha_api_key=body.bocha_api_key, base_url=body.base_url,
                model=body.model, current=settings, env_path=Path(_ENV_FILE),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="保存配置失败，请检查后端写入权限") from exc
        clear_settings_cache()
        llm_core.reset_client()
        updated = get_settings()
    response.headers["Cache-Control"] = "no-store"
    return _config_payload(updated, True)


@app.get("/api/search")
def search_endpoint(request: Request, q: str, num: int = 10, site: Optional[str] = None):
    with use_request_settings(_request_settings(request)):
        return _search_endpoint(q, num, site)


def _search_endpoint(q: str, num: int, site: Optional[str]):
    try:
        results = search(q, num=num, site=site)
        return {"ok": True, "query": q, "site": site, "results": results}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "query": q, "reason": "error", "message": str(e)}


# ── 专家 ────────────────────────────────────────────────
@app.get("/api/experts")
def list_experts():
    return load_experts()


@app.get("/api/experts/workload")
def experts_workload():
    """专家工作量看板：真实累计任务/产出论点/采集证据。"""
    stats = {s["expert_id"]: s for s in db.expert_workload()}
    out = []
    for e in load_experts():
        s = stats.get(e["id"])
        out.append({
            "id": e["id"],
            "name": e.get("name", e["id"]),
            "title": e.get("role_title", ""),
            "layer": e.get("level", ""),
            "avatar": e.get("avatar", ""),
            "missions": s["missions"] if s else 0,
            "claims_authored": s["claims_authored"] if s else 0,
            "evidence_collected": s["evidence_collected"] if s else 0,
            "last_active": s["last_active"] if s else "",
        })
    out.sort(key=lambda x: (x["missions"], x["claims_authored"], x["evidence_collected"]), reverse=True)
    return out


@app.get("/api/experts/{eid}")
def get_expert(eid: str):
    e = expert_by_id(eid)
    if not e:
        return {"ok": False, "message": "not found"}
    stat = next((s for s in db.expert_workload() if s["expert_id"] == eid), None)
    return {**e, "stats": stat or {"missions": 0, "claims_authored": 0, "evidence_collected": 0, "last_active": ""}}


# ── 任务 / 澄清 ─────────────────────────────────────────
class CreateTaskBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: str = "deep"  # quick | deep | expert


@app.post("/api/tasks")
def post_task(body: CreateTaskBody, request: Request):
    with use_request_settings(_request_settings(request)):
        settings = get_settings()
        if not settings.llm_configured:
            raise HTTPException(status_code=503, detail="当前模型服务未配置 API Key、网关或模型")
        if not settings.bocha_api_key:
            raise HTTPException(status_code=503, detail="未配置 BOCHA_API_KEY，无法进行真实联网调研")
        query = body.query.strip()
        if not query:
            raise HTTPException(status_code=422, detail="调研主题不能为空")
        return create_task(query, mode=body.mode)


class ClarifyBody(BaseModel):
    answers: dict = {}


@app.post("/api/tasks/{task_id}/clarify")
def post_clarify(task_id: str, body: ClarifyBody):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return submit_clarify(task_id, body.answers)


# ── SSE 思维流 ──────────────────────────────────────────
@app.get("/api/tasks/{task_id}")
def task_state(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {**task, "execution": task_runtime.state(task_id)}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        await task_runtime.start(task_id, request.state.visitor_id, _request_settings(request),
                                 run_pipeline, retry=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    return {"ok": True}


@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request, sub_id: str = "", after: int = 0):
    visitor_id = request.state.visitor_id
    with db.use_visitor(visitor_id):
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if not task_runtime.state(task_id) and task.get("status") != "done":
            try:
                await task_runtime.start(task_id, visitor_id, _request_settings(request), run_pipeline, sub_id=sub_id)
            except RuntimeError as exc:
                raise HTTPException(status_code=429, detail=str(exc))
    async def gen():
        with db.use_visitor(visitor_id):
            if task.get("status") == "done" and not task_runtime.state(task_id):
                yield f"event: done\ndata: {json.dumps({'reportId': task.get('report_id')})}\n\n"
                return
            async for event in task_runtime.observe(task_id, request, after=max(0, after)):
                yield event
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 报告 / 历史（持久化）─────────────────────────────────
@app.get("/api/reports")
def list_reports():
    """我的调研：真实历史报告列表（卡片，不含全文）。"""
    return db.list_reports()


@app.get("/api/reports/{report_id}")
def get_report(report_id: str):
    rep = db.get_report(report_id)
    if not rep:
        return {"ok": False, "message": "report not ready"}
    return rep


# ── 可观测性 Trace（决策链路 / 决策回放）──────────────────
@app.get("/api/tasks/{task_id}/trace")
def get_task_trace(task_id: str):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    from app.core import trace as _trace
    spans = _trace.get_trace(task_id) or db.get_traces_by_task(task_id)
    return {"taskId": task_id, "spans": spans}


@app.get("/api/reports/{report_id}/trace")
def get_report_trace(report_id: str):
    if not db.get_report(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    spans = db.get_traces_by_report(report_id)
    if not spans:
        rep = db.get_report(report_id)
        spans = (rep or {}).get("trace", [])
    return {"reportId": report_id, "spans": spans}


# ── 报告反馈（人工修正率 → 业务闭环指标）──────────────────
class FeedbackBody(BaseModel):
    edited_blocks: int = 0
    total_blocks: int = 0
    data: dict = {}


@app.post("/api/reports/{report_id}/feedback")
def post_feedback(report_id: str, body: FeedbackBody):
    if not db.get_report(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    db.save_report_feedback(report_id, body.edited_blocks, body.total_blocks, body.data)
    # 同步更新报告内 metrics 的人工修正率
    rep = db.get_report(report_id)
    if rep and rep.get("metrics"):
        from app.core.metrics import apply_feedback
        rep["metrics"] = apply_feedback(rep["metrics"], body.edited_blocks, body.total_blocks)
        db.save_report(rep, task_id="")
    return {"ok": True}


# ── 按批注深化章节（人工介入二次调研）────────────────────
class RefineBody(BaseModel):
    section_id: str
    annotations: List[str] = []


@app.post("/api/reports/{report_id}/refine")
def post_refine(report_id: str, body: RefineBody, request: Request):
    if not db.get_report(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    with use_request_settings(_request_settings(request)):
        with llm_core.use_request_client():
            return refine_section(report_id, body.section_id, body.annotations)


# ── 仪表盘（真实统计）───────────────────────────────────
@app.get("/api/dashboard")
def dashboard():
    return db.dashboard_stats()


@app.get("/api/performance")
def performance_summary():
    return db.performance_summary()


# ── 全局证据溯源库 ──────────────────────────────────────
@app.get("/api/evidences")
def evidences(
    brand: Optional[str] = None,
    source_type: Optional[str] = None,
    min_cred: float = 0.0,
    limit: int = 200,
):
    items = db.query_evidences(brand=brand, source_type=source_type, min_cred=min_cred, limit=limit)
    return {"items": items, "facets": db.evidence_facets()}


# ── 竞品监控订阅 ────────────────────────────────────────
class SubscriptionBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    brands: List[str] = Field(default_factory=list, max_length=20)


@app.get("/api/subscriptions")
def list_subscriptions():
    return db.list_subscriptions()


@app.post("/api/subscriptions")
def create_subscription(body: SubscriptionBody):
    import uuid
    sub_id = f"sub_{uuid.uuid4().hex}"
    return db.create_subscription(sub_id, body.query, body.brands)


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: str):
    if not db.get_subscription(sub_id):
        raise HTTPException(status_code=404, detail="订阅不存在")
    db.delete_subscription(sub_id)
    return {"ok": True}
