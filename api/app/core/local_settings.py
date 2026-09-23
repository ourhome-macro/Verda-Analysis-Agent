"""仅供本机开发使用的密钥配置写入；接口永不返回密钥值。"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import Settings


_WRITE_LOCK = threading.Lock()
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=:-]{1,2048}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=")
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def local_edit_allowed(settings: Settings, client_host: str, origin: str = "") -> bool:
    """同时要求显式开关、本机连接和可信浏览器来源。"""
    if not settings.local_settings_enabled:
        return False
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return False
    if client_host not in _LOCAL_HOSTS:
        return False
    allowed_origins = {
        settings.frontend_origin.rstrip("/"),
        "http://localhost:3400",
        "http://127.0.0.1:3400",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    }
    return not origin or origin.rstrip("/") in allowed_origins


def _key(value: str | None, label: str) -> str:
    cleaned = (value or "").strip()
    if cleaned and not _KEY_PATTERN.fullmatch(cleaned):
        raise ValueError(f"{label} 格式不正确：不能包含空白、引号或控制字符")
    return cleaned


def _url(value: str | None) -> str:
    cleaned = (value or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if len(cleaned) > 2048:
        raise ValueError("自定义网关地址过长")
    if any(char.isspace() or char == "\0" for char in cleaned):
        raise ValueError("自定义网关地址不能包含空白或控制字符")
    parsed = urlparse(cleaned)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"http", "https"}):
        raise ValueError("自定义网关必须是有效的 HTTP(S) 地址，且不能包含账号、查询参数或片段")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError("非本机自定义网关必须使用 HTTPS")
    return cleaned


def _model(value: str | None) -> str:
    cleaned = (value or "").strip()
    if cleaned and not _MODEL_PATTERN.fullmatch(cleaned):
        raise ValueError("模型名只能包含字母、数字、点、下划线、斜线、冒号和短横线")
    return cleaned


def _atomic_update_env(path: Path, updates: dict[str, str]) -> None:
    if path.is_symlink():
        raise ValueError("拒绝写入符号链接形式的 .env 文件")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines: list[str] = []
    replaced: set[str] = set()
    for line in original.splitlines():
        match = _ENV_LINE.match(line)
        name = match.group(1) if match else ""
        if name in updates:
            if name not in replaced:
                lines.append(f"{name}={updates[name]}")
                replaced.add(name)
            continue
        lines.append(line)
    for name, value in updates.items():
        if name not in replaced:
            lines.append(f"{name}={value}")
    payload = newline.join(lines).rstrip("\r\n") + newline
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=".env-", suffix=".tmp", delete=False) as temp:
            temp.write(payload)
            temp_path = temp.name
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def save_local_credentials(
    *, provider: str, api_key: str | None, bocha_api_key: str | None,
    base_url: str | None, model: str | None, current: Settings, env_path: Path,
) -> None:
    """空密钥表示保留现有值；切换 Provider 时清除不适用的通用覆盖项。"""
    if provider not in {"deepseek", "zhipu", "custom"}:
        raise ValueError("不支持的模型服务")
    new_key = _key(api_key, "模型 API Key")
    new_bocha = _key(bocha_api_key, "博查 API Key")
    new_url = _url(base_url)
    new_model = _model(model)
    switched = provider != current.llm_provider
    updates = {"LLM_PROVIDER": provider}
    if switched:
        updates.update({"LLM_MODEL": "", "LLM_MODEL_CORE": "",
                        "LLM_MODEL_AUX": "", "LLM_MODEL_FAST": ""})
    if provider == "custom":
        if switched and not new_key:
            raise ValueError("切换到自定义模型服务时必须填写 API Key")
        if not (new_url or (not switched and current.llm_base_url)):
            raise ValueError("自定义模型服务需要网关地址")
        if not (new_model or (not switched and current.llm_model)):
            raise ValueError("自定义模型服务需要默认模型名")
        if new_key:
            updates["LLM_API_KEY"] = new_key
        if new_url:
            updates["LLM_BASE_URL"] = new_url
        if new_model:
            updates["LLM_MODEL"] = new_model
    else:
        if new_url or new_model:
            raise ValueError("内置模型服务的网关和模型请通过 .env 高级配置修改")
        specific_name = "DEEPSEEK_API_KEY" if provider == "deepseek" else "ZHIPU_API_KEY"
        stored_key = current.deepseek_api_key if provider == "deepseek" else current.zhipu_api_key
        if switched and not (new_key or stored_key):
            raise ValueError(f"切换到 {provider} 时必须填写模型 API Key")
        if switched or new_key:
            updates["LLM_API_KEY"] = ""
            updates["LLM_BASE_URL"] = ""
        if new_key:
            updates[specific_name] = new_key
    if new_bocha:
        updates["BOCHA_API_KEY"] = new_bocha
    for name, value in updates.items():
        if name in os.environ and os.environ[name] != value:
            raise ValueError(f"{name} 由进程环境变量控制，请在运行环境中修改")
    with _WRITE_LOCK:
        _atomic_update_env(env_path, updates)
