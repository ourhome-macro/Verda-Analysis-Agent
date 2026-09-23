# Docker Hub 镜像发布记录（2026-09-17）

> 本页记录的 `2026-09-17-1` 标签属于旧版：它要求服务器预填模型与博查 Key，不支持访客在浏览器自行保存 Key。新版行为与上线条件见 [访客 API Key 部署方案](./BROWSER_API_KEYS_DEPLOYMENT.md)；新镜像发布前不要把旧标签当作新版部署。

公开仓库：

| 服务 | 固定标签 | 平台 | 多架构 manifest digest |
|---|---|---|---|
| 后端 | `macrohome/verda-backend:2026-09-17-1` | `linux/amd64`, `linux/arm64` | `sha256:356c4b50a42d0958cdfeaac7b02eead35fde577943e881784aa3746a2223465a` |
| 前端 | `macrohome/verda-web:2026-09-17-1` | `linux/amd64`, `linux/arm64` | `sha256:b54df3ab9f8842e5f2ebf533b7bb3587d36481908869f37b8932838f0f885641` |

`-amd64` 标签也已发布，用于需要明确指定 x86_64 镜像的场景。镜像不含本机 `.env`、SQLite 数据库或登录密码；服务器仍需提供 `backend/.env` 与 `deploy.env`。公网仓库无需登录即可拉取。
两个固定标签已通过 Docker Hub 匿名接口核对公开状态、manifest digest 与 amd64/arm64 平台，并用 `docker compose --env-file deploy.hub.env.example pull backend web` 实际拉取成功。

最小服务器文件集：`compose.yaml`、`deploy.hub.env.example`、`backend/.env.example`。复制两个示例文件为 `deploy.env`、`backend/.env`，填入访问密码哈希与 API Key，再运行：

```bash
docker compose --env-file deploy.env config --quiet
docker compose --env-file deploy.env pull backend web
docker compose --env-file deploy.env up -d --no-build
```

详细的 SSH 上传、密码生成、私网访问和域名 HTTPS 步骤见 [DOCKER_DEPLOYMENT.md](./DOCKER_DEPLOYMENT.md)。
