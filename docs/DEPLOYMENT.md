# 配置与部署

## 本地开发

从 `backend/.env.example` 复制 `backend/.env`。本机服务端凭据可填写 `DEEPSEEK_API_KEY` 和 `BOCHA_API_KEY`；使用自定义 OpenAI 兼容网关时设置 `LLM_PROVIDER=custom`、`LLM_API_KEY`、`LLM_BASE_URL` 和模型名。`backend/.env` 被忽略，不得提交。

后端依赖在根目录 `requirements.txt`；容器另安装 Uvicorn。前端在 `frontend/` 执行 `npm ci` 和 `npm run dev`，Vite 默认端口 3400，`/api` 代理至本机 8010。具体命令见根目录 [README](../README.md)。

## Docker Compose

先准备 `backend/.env` 与 `deploy.env`：

```powershell
Copy-Item backend/.env.example backend/.env
Copy-Item deploy.env.example deploy.env
docker compose --env-file deploy.env up -d --build
```

Compose 把 `backend/.env` 作为文件型 Secret 挂载，数据保存在 `verda_data` 卷，Web 由 Caddy 反向代理。当前 Compose 设置 `REQUIRE_CLIENT_API_KEYS=true`：部署时前端访客需要提供自己的 DeepSeek 与博查 Key。默认 `PUBLISH_HOST=127.0.0.1`，公网访问需配置域名和网络入口。

后端固定单 worker。分层出站限流与 SQLite 状态只保证该进程范围内的约束；要横向扩容，先设计共享任务租约、数据库与分布式限流。

## Vercel

`api/index.py` 是单独的 Serverless 入口。Serverless 实例间不共享进程内限流、Trace 缓冲和本地 SQLite；该路径不具备与单 worker Docker 相同的持久执行保证。应根据部署目标先补共享状态服务，而不是把单机容量上限直接视为全局配额。
