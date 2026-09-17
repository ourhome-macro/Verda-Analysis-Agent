# LLM Provider 与本地联调（2026-09-17）

## 本次变更

- 后端统一由 `LLM_PROVIDER` 选择 `deepseek`、`zhipu` 或 `custom`；模型、网关和密钥在 `backend/app/core/config.py` 集中解析。
- `llm.py` 使用当前 Provider 调用 OpenAI 兼容 Chat Completions。DeepSeek 默认使用 `deepseek-flash`，核心章节默认 `deepseek-v4-pro`；现有 JSON 输出流程统一关闭思考模式。
- `GET /api/llm/config` 仅公开 Provider 和模型档位，不返回密钥；首页展示实际配置，移除了原先不生效的 GLM 模型下拉。
- 任务创建前检查 LLM 和博查密钥。缺少配置时返回明确的 503，前端显示错误，不再生成虚假的演示任务 ID。
- 本地后端 `backend/app` 与 Vercel 镜像 `api/app` 的对应代码已同步。

## 配置 DeepSeek

编辑未跟踪的 `backend/.env`：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=填写你自己的密钥
BOCHA_API_KEY=填写博查密钥
```

`LLM_MODEL_CORE=deepseek-flash` 可在联调时让所有档位使用 Flash。若使用第三方 OpenAI 兼容服务，设置 `LLM_PROVIDER=custom`，并填写 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`。修改 `.env` 后重启后端。

DeepSeek 模型和网关以 [官方接入说明](https://api-docs.deepseek.com/) 为准。

## 本地运行与验证

在 `backend/` 使用 `.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010`，在 `frontend/` 使用 `npm.cmd run dev`（端口 3400）。Windows 下安装后端依赖时使用 `python -X utf8 -m pip install -r requirements.txt`。

本次本机验证：Python 3.10 后端依赖安装成功；Node 24 下前端 TypeScript 与 Vite 构建成功；三个 Provider 的模型选择和请求参数通过无网络模拟测试；后端 `/health`、`/api/llm/config`，前端首页与 Vite API 代理均返回正常；未配置 Key 时 `/api/tasks` 返回 503。当前 `.env` 的密钥字段为空，因此尚未验证真实模型调用和博查搜索。
