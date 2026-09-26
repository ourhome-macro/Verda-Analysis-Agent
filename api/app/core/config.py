"""全局配置：统一选择 OpenAI 兼容的模型服务，密钥只从环境读取。"""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# 用绝对路径定位 backend/.env，避免因启动工作目录不同而读不到密钥。
# config.py 位于 backend/app/core/，向上三级即 backend/。
_BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_ENV_FILE = os.environ.get("VERDA_ENV_FILE") or os.path.join(_BACKEND_DIR, ".env")


@dataclass(frozen=True)
class LLMProviderConfig:
    name: str
    api_key: str
    base_url: str
    default_model: str
    core_model: str
    aux_model: str
    fast_model: str

    def model_for(self, tier: str) -> str:
        return {
            "core": self.core_model,
            "aux": self.aux_model,
            "fast": self.fast_model,
        }.get(tier, self.default_model)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # 统一入口。LLM_MODEL* 可覆盖当前 Provider 的默认模型。
    llm_provider: Literal["zhipu", "deepseek", "custom"] = "deepseek"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_model_core: str = ""
    llm_model_aux: str = ""
    llm_model_fast: str = ""

    # 智谱兼容旧配置
    zhipu_api_key: str = ""
    # 默认/杂务模型：glm-5.1（10 并发，质量高、速度快）。
    # 报告章节按 SECTION_MODEL_MAP 用 glm-5.2(核心)/glm-5.1(辅助)；
    # intake/澄清/情感分类等杂务用 zhipu_model_fast（高并发极速）。
    zhipu_model: str = "glm-5.1"
    # 核心章模型（质量最高，10 并发）
    zhipu_model_core: str = "glm-5.2"
    # 辅助章模型（质量高，10 并发）
    zhipu_model_aux: str = "glm-5.1"
    # 杂务/快速模型（30 并发，极速，用于澄清/情感分类/单条重写等轻任务）
    zhipu_model_fast: str = "glm-z1-air"
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # DeepSeek：默认关闭思考模式以适配现有 JSON 解析流程。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # 单次上游调用超时；SDK 内置重试已禁用，由 llm_attempts 显式控制。
    llm_timeout: float = 180.0
    llm_max_retries: int = 0  # retained for old environment files; ignored
    llm_max_input_bytes: int = 180000
    # All limits apply to one serving process. Disable SDK's hidden retries in favor of these.
    outbound_global_requests_per_second: float = 4.0
    outbound_global_max_in_flight: int = 6
    llm_requests_per_second: float = 2.0
    llm_max_in_flight: int = 3
    llm_max_queue_wait: float = 90.0
    llm_attempts: int = 3
    llm_retry_base_seconds: float = 1.0
    llm_retry_max_seconds: float = 20.0
    write_batch_size: int = 3
    analyze_batch_size: int = 3

    # 搜索 API（博查 Bocha Web Search：https://open.bocha.cn 获取 key）
    bocha_api_key: str = ""
    bocha_base_url: str = "https://api.bocha.cn/v1"
    anysearch_api_key: str = ""
    anysearch_base_url: str = "https://api.anysearch.com/v1"
    grok_search_api_key: str = ""
    grok_search_base_url: str = "https://api.x.ai/v1"
    grok_search_model: str = "grok-4.7"
    grok_search_mode: str = "native"  # native=服务端 web_search；planner=模型规划查询词+AnySearch 取证
    search_providers: str = "bocha"
    bocha_requests_per_second: float = 2.0
    anysearch_requests_per_second: float = 2.0
    grok_requests_per_second: float = 1.0
    search_max_in_flight: int = 2
    search_max_queue_wait: float = 60.0
    search_attempts: int = 3
    search_retry_base_seconds: float = 1.0
    search_retry_max_seconds: float = 20.0
    collect_brand_concurrency: int = 2
    # 单次搜索超时（秒）
    search_timeout: float = 30.0
    # 兼容旧字段（已弃用，不再使用）
    serpapi_key: str = ""
    bing_search_key: str = ""

    # 平台采集
    douyin_cookie: str = ""
    xhs_cookie: str = ""
    bilibili_cookie: str = ""

    # 服务
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    enable_demo_fallback: bool = True
    local_settings_enabled: bool = False
    require_client_api_keys: bool = False

    @property
    def provider_config(self) -> LLMProviderConfig:
        if self.llm_provider == "deepseek":
            key = self.llm_api_key or self.deepseek_api_key
            base_url = self.llm_base_url or self.deepseek_base_url
            defaults = ("deepseek-flash", "deepseek-v4-pro", "deepseek-flash", "deepseek-flash")
        elif self.llm_provider == "custom":
            key = self.llm_api_key
            base_url = self.llm_base_url
            defaults = ("", "", "", "")
        else:
            key = self.llm_api_key or self.zhipu_api_key
            base_url = self.llm_base_url or self.zhipu_base_url
            defaults = (self.zhipu_model, self.zhipu_model_core,
                        self.zhipu_model_aux, self.zhipu_model_fast)
        default_model = self.llm_model or defaults[0]
        return LLMProviderConfig(
            name=self.llm_provider,
            api_key=key,
            base_url=base_url,
            default_model=default_model,
            core_model=self.llm_model_core or defaults[1] or default_model,
            aux_model=self.llm_model_aux or defaults[2] or default_model,
            fast_model=self.llm_model_fast or defaults[3] or default_model,
        )

    @property
    def llm_configured(self) -> bool:
        provider = self.provider_config
        return bool(provider.api_key and provider.base_url and provider.default_model)


_REQUEST_SETTINGS: ContextVar[Settings | None] = ContextVar("verda_request_settings", default=None)


@lru_cache
def _base_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    return _REQUEST_SETTINGS.get() or _base_settings()


def clear_settings_cache() -> None:
    _base_settings.cache_clear()


def has_request_settings() -> bool:
    return _REQUEST_SETTINGS.get() is not None


@contextmanager
def use_request_settings(settings: Settings | None):
    if settings is None:
        yield
        return
    token = _REQUEST_SETTINGS.set(settings)
    try:
        yield
    finally:
        _REQUEST_SETTINGS.reset(token)
