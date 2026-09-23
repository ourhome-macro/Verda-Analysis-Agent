# 旧版后端 unhealthy 与 Web 端口冲突排查

本文前半部分记录 `2026-09-17-1` 旧镜像的故障。新版访客自带 Key 模式已取消“服务器未填 API Key 则不健康”的限制，见 [新版部署方案](./BROWSER_API_KEYS_DEPLOYMENT.md)。下文的 1Panel 端口冲突与 401 验收仍适用于新版。

适用现象：`docker compose up -d --no-build` 报 `dependency failed to start: container verda-backend-1 is unhealthy`，`docker compose ps` 只有 `backend`，服务器本机访问 `127.0.0.1:8088` 被拒绝连接。

本次部署已确认：服务器没有填写模型 API Key 和博查搜索 API Key，因此两项配置检查均无法通过。补齐服务器上的 `backend/.env` 后重建后端容器即可解除该阻塞；还需按下文验证容器健康与 Web 端口。

后续验证已确认 `backend` 变为 `healthy`，但 Web 启动时报 `failed to bind host port 127.0.0.1:80/tcp: address already in use`。这是独立的宿主机端口冲突：`deploy.env` 仍让 Web 占用 80，而服务器已有进程占用该端口。1Panel 反向代理模式应按下方步骤将 Web 发布到本机 8088。

## 原因链

旧版 `compose.yaml` 要求 `web` 等待 `backend` 通过健康检查。旧版后端健康检查访问容器内的 `http://127.0.0.1:8000/health`，并要求响应里的 `llm_configured` 和 `search_configured` 都为 `true`。任一条件不满足，`web` 就不会启动，也不会监听宿主机的 8088 端口。单独执行 `up ... backend` 显示 `Started`，只说明容器进程已启动，不能证明健康检查通过。

旧版健康日志可能只有 `AssertionError`，此时以下方 `/health` 响应为准。新版 Compose 的健康检查仅验证后端响应，不再判断 API Key 是否存在。

## 在服务器上定位

在 `/opt/verda` 执行以下命令；不要粘贴 `backend/.env`、完整环境变量、API Key 或访问密码哈希到工单中。

```bash
docker compose --env-file deploy.env ps -a
docker compose --env-file deploy.env logs --tail=100 backend
docker inspect verda-backend-1 --format '{{json .State.Health}}'
docker compose --env-file deploy.env exec -T backend python -c 'from app.core.config import get_settings; s=get_settings(); print(s.llm_provider, s.llm_configured, bool(s.bocha_api_key))'
docker compose --env-file deploy.env exec -T backend python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())'
```

每条命令应单独执行，不要把 Markdown 反引号或中文分号粘入 Shell。第四条只输出 `provider`、模型配置布尔值和搜索配置布尔值，不泄露密钥。若最后一条返回类似 `{"status":"ok","provider":"deepseek","llm_configured":false,"search_configured":false}`，说明后端已响应，但业务密钥未配置或未被读取。模型与搜索密钥应写入服务器的 `/opt/verda/backend/.env`；`deploy.env` 只负责 Compose 镜像、端口和 Web 访问配置。`LLM_PROVIDER=deepseek` 时需有效的 `DEEPSEEK_API_KEY`（也可使用 `LLM_API_KEY`）；`LLM_PROVIDER=zhipu` 时需 `ZHIPU_API_KEY`（也可使用 `LLM_API_KEY`）；搜索还需 `BOCHA_API_KEY`。仅有非空值可以通过本地健康检查，密钥真实可用性仍需后续接口验证。

修改 `backend/.env` 后重新创建后端，使文件型 Secret 重新挂载。等后端显示 `healthy` 再启动 Web：

```bash
chmod 600 backend/.env
docker compose --env-file deploy.env up -d --no-build --force-recreate backend
docker compose --env-file deploy.env ps
docker compose --env-file deploy.env up -d --no-build web
docker compose --env-file deploy.env ps
curl -I http://127.0.0.1:8088/
```

若 `backend` 仍是 `unhealthy`，不要反复重建。结合健康检查输出判别：

- 容器内 `/health` 连接失败：看后端日志中的启动异常、Secret 文件读取错误或数据库权限错误。
- `/health` 返回 `llm_configured:false` 或 `search_configured:false`：检查对应配置名称和服务器上的 `backend/.env`，修改后重新创建容器。
- `/health` 两项都是 `true` 但仍不健康：查看 `docker inspect` 中的健康检查命令、退出码与输出，确认服务器的 `compose.yaml` 和镜像版本与部署文件一致。

本部署方案若 `SITE_ADDRESS=:80` 且 `PUBLISH_HTTP_PORT=8088`，Web 就绪后未携带 Basic Auth 的 `curl -I` 预期返回 `401`。若 `ps` 显示 Web 已启动但仍无法连接，再检查 `deploy.env` 中的 `PUBLISH_HOST`、`PUBLISH_HTTP_PORT` 以及 Web 日志。不要为了绕过此故障删除健康检查或把后端 8000 端口直接暴露到公网。

## 1Panel 占用 80/443 后的 Web 端口冲突

三个文件的职责必须分开：`backend/.env` 放模型与搜索密钥及后端配置；`deploy.env` 放 `SITE_ADDRESS`、`PUBLISH_*`、`BASIC_AUTH_*` 和镜像名；`compose.yaml` 从 `name: verda` 开始，包含 `services:`。不要把 `SITE_ADDRESS=:80` 接在 `FRONTEND_ORIGIN` 值的末尾，也不要把 `name: verda` 接在 `BASIC_AUTH_USER` 值的末尾。`backend/.env` 示例中的 `APP_PORT=8010` 用于本地运行；容器里的端口由 Compose 环境变量和 Uvicorn 启动命令设置为 8000，与宿主机 Web 端口冲突无关。

如需确认两个环境文件的非敏感字段分别在哪里，可执行以下命令；输出不包含 API Key 或 Basic Auth 哈希：

```bash
grep -nE '^(APP_HOST|APP_PORT|FRONTEND_ORIGIN|SITE_ADDRESS|PUBLISH_HOST|PUBLISH_HTTP_PORT|PUBLISH_HTTPS_PORT|BASIC_AUTH_USER)=' backend/.env deploy.env
grep -nE '^(name:|services:)' backend/.env deploy.env compose.yaml
```

在服务器 `/opt/verda/deploy.env` 中确认只有一组以下设置（保留原有镜像和 Basic Auth 配置）：

```dotenv
SITE_ADDRESS=:80
PUBLISH_HOST=127.0.0.1
PUBLISH_HTTP_PORT=8088
PUBLISH_HTTPS_PORT=18443
```

`PUBLISH_HTTPS_PORT` 也要改开，避免解决 80 冲突后又碰到 443 冲突。保存后执行：

```bash
docker compose --env-file deploy.env config --quiet
docker compose --env-file deploy.env up -d --no-build --force-recreate web
docker compose --env-file deploy.env ps
curl -I http://127.0.0.1:8088/
```

预期 `web` 为运行状态，`curl` 返回 `401`。若 8088 或 18443 已被占用，先用 `ss -ltnp` 查看占用进程，选用实际空闲的本机高位端口，并同步修改 1Panel 的反向代理地址；不要停止现有的 80/443 服务。

## 收到 401 后完成访问验收

`curl -I http://127.0.0.1:8088/` 返回 `401` 表示 Web 已监听，Caddy 的 Basic Auth 正在拦截未认证请求。这不是启动失败。下一步在 1Panel 中给目标域名配置反向代理，上游地址填写 `http://127.0.0.1:8088`，由 1Panel 负责公网 HTTPS 证书和 80/443 入口。确认域名已解析到服务器，再用浏览器打开 `https://你的域名`，输入 `deploy.env` 中的 `BASIC_AUTH_USER` 和生成哈希时使用的原始密码。

登录后检查首页与 `/health`，后者应返回 `status: ok`。旧版由服务器提供 Key 时，两项配置布尔值应为 `true`；新版访客自带 Key 时，服务器不保存访客 Key，因此这两项可以保持 `false`，以页面通知栏消失和任务创建验收为准。配置状态不能证明外部 API 一定可用；正式使用前还应做一次小规模的模型和搜索功能验证。若 1Panel 返回 `502`，先检查其 OpenResty 运行网络是否能访问服务器本机的 `127.0.0.1:8088`，不要直接把 8088 改成公网监听来绕过问题。
