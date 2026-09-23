"""Build an isolated, request-scoped DeepSeek/Bocha configuration from browser headers."""
from __future__ import annotations

import re
from typing import Mapping

from app.core.config import Settings


_KEY = re.compile(r"^[A-Za-z0-9._~+/=:-]{1,2048}$")


def browser_settings(headers: Mapping[str, str], base: Settings) -> Settings | None:
    llm_key = headers.get("x-verda-deepseek-key", "").strip()
    bocha_key = headers.get("x-verda-bocha-key", "").strip()
    if not llm_key and not bocha_key:
        if base.require_client_api_keys:
            raise ValueError("请先在页面填写 DeepSeek 和博查 API Key")
        return None
    if not _KEY.fullmatch(llm_key) or not _KEY.fullmatch(bocha_key):
        raise ValueError("请填写有效的 DeepSeek 和博查 API Key")
    # Clear all server-level provider overrides so a visitor cannot use another key.
    return base.model_copy(update={
        "llm_provider": "deepseek",
        "llm_api_key": llm_key,
        "deepseek_api_key": "",
        "zhipu_api_key": "",
        "llm_base_url": "",
        "deepseek_base_url": "https://api.deepseek.com",
        "llm_model": "",
        "llm_model_core": "",
        "llm_model_aux": "",
        "llm_model_fast": "",
        "bocha_api_key": bocha_key,
        "bocha_base_url": "https://api.bocha.cn/v1",
    })
