# Docker Compose 服务器部署

本方案面向一台 Linux 服务器。`backend` 运行 FastAPI 单进程，`web` 用 Caddy 托管前端、代理 `/api/*` 和 `/health`，并对整个工作台加访问密码。SQLite 位于 `verda_data` 持久卷；后端不发布端口。默认仅在服务器本机发布 Web 端口，使用 SSH 隧道访问。拥有域名时可开放公网，由 Caddy 自动申请和续期 HTTPS 证书。

Compose 同时支持两种交付方式：服务器收到源码后执行 `up -d --build`；或者从 Docker Hub 直接拉取预构建镜像，执行 `pull` 和 `up -d --no-build`。后者只需要 `compose.yaml`、服务器上的 `backend/.env` 和 `deploy.env`，不需要传 `api/`、`frontend/src/`、Dockerfile 或 Node/Python 依赖目录。

## 1. 服务器准备

安装 Docker Engine 与 Compose 插件，并确认 `docker compose version` 可运行。建议使用 Linux 主机和至少 4 GiB 内存完成镜像构建。公网模式还需要域名 A 记录指向服务器，并在云安全组与防火墙开放 TCP 80/443；UDP 443 用于 HTTP/3，可选。

Docker 安装以 [Docker 官方安装指南](https://docs.docker.com/engine/install/) 为准。Caddy 的 [自动 HTTPS 条件](https://caddyserver.com/docs/automatic-https) 包括正确的 DNS、可达的 80/443 端口与持久化证书数据目录。

## 2. 从 Windows 上传代码

在项目根目录的 PowerShell 中，先确认 `.env`、`.venv`、`node_modules` 等没有进入版本控制，再提交要部署的代码：

```powershell
git status --short
git add .
git status --short
git commit -m "chore: add Docker deployment"
git archive --format=tar.gz --output=verda-deploy.tar.gz HEAD
scp .\verda-deploy.tar.gz USER@SERVER_IP:/tmp/
```

`git archive` 只打包已提交文件，不会打包被 Git 忽略的本机密钥、虚拟环境和数据库。把 `USER`、`SERVER_IP` 换成实际 SSH 用户和服务器地址。压缩包被 `.gitignore` 忽略。
仓库的 `.gitattributes` 固定 Linux 启动脚本和 Docker 配置使用 LF 换行，避免从 Windows 打包后出现 `/bin/sh^M` 错误。

在服务器上：

```bash
ssh USER@SERVER_IP
sudo mkdir -p /opt/verda
sudo chown "$USER:$USER" /opt/verda
tar -xzf /tmp/verda-deploy.tar.gz -C /opt/verda
cd /opt/verda
```

## 3. 服务器密钥与访问密码

在服务器上单独创建密钥文件，不把本机 `backend/.env` 打进镜像或 Git：

```bash
cp backend/.env.example backend/.env
nano backend/.env
chmod 600 backend/.env
```

新版访客自带 Key 模式下，`backend/.env` 文件仍需存在，但 `DEEPSEEK_API_KEY` 和 `BOCHA_API_KEY` 可留空；访客在网页上填写自己的 Key。保持 `LOCAL_SETTINGS_ENABLED=false`。Compose 把该文件作为只读 Secret 挂入后端；启动脚本把它复制到容器临时目录、限制为应用用户可读，再降权启动 FastAPI。这样宿主机文件可保持 `600`。上线版本要求见 [访客 API Key 部署方案](./BROWSER_API_KEYS_DEPLOYMENT.md)。

生成工作台访问密码的哈希：

```bash
docker run --rm -it caddy:2.11.4-alpine caddy hash-password
cp deploy.env.example deploy.env
nano deploy.env
chmod 600 deploy.env
```

在 `deploy.env` 中把 `BASIC_AUTH_HASH` 填为生成的哈希，**用单引号包住整个哈希**，例如 `BASIC_AUTH_HASH='$2a$...'`，防止 Compose 把 `$` 当作变量展开。`BASIC_AUTH_USER` 是浏览器登录时输入的用户名。

## 4. 启动与访问

先验证 Compose 配置，再构建并启动：

```bash
docker compose --env-file deploy.env config --quiet
docker compose --env-file deploy.env up -d --build
docker compose --env-file deploy.env ps
docker compose --env-file deploy.env logs --tail=100 backend web
```

### 只有服务器 IP：SSH 隧道

保持 `deploy.env` 默认的 `SITE_ADDRESS=:80`、`PUBLISH_HOST=127.0.0.1`。在本机 PowerShell 新终端运行：

```powershell
ssh -N -L 8080:127.0.0.1:80 USER@SERVER_IP
```

浏览器打开 `http://127.0.0.1:8080`。网页流量经 SSH 加密；服务器不会对公网开放工作台端口。请保持该 SSH 窗口运行。

### 有域名：公网 HTTPS

将 `deploy.env` 改为：

```dotenv
SITE_ADDRESS=research.example.com
PUBLISH_HOST=0.0.0.0
PUBLISH_HTTP_PORT=80
PUBLISH_HTTPS_PORT=443
```

替换为自己的域名，确认 DNS 与防火墙后再次运行 `docker compose --env-file deploy.env up -d`。浏览器访问 `https://research.example.com`，Caddy 自动管理证书并要求输入工作台账号密码。不要用公网明文 HTTP 传 Basic Auth 密码。

### 使用 1Panel 创建反向代理网站

如果 1Panel OpenResty 已占用宿主机 80/443，先让 Docker Web 服务只绑定本机高位端口，并由 1Panel 负责公网 HTTPS。在服务器的 `deploy.env` 设置：

```dotenv
SITE_ADDRESS=:80
PUBLISH_HOST=127.0.0.1
PUBLISH_HTTP_PORT=8088
PUBLISH_HTTPS_PORT=18443
```

运行 `docker compose --env-file deploy.env up -d --no-build --force-recreate web`。随后在 1Panel「反向代理」网站的 **代理地址** 填完整 URL：`http://127.0.0.1:8088`；备注可写“青野 Verda”。先在服务器运行 `curl -I http://127.0.0.1:8088/`，预期返回 `401`，表示 Caddy 已就绪且访问密码生效。若返回连接失败，先检查 `docker compose --env-file deploy.env ps`；若 1Panel 返回 502，确认 OpenResty 使用默认 host 网络模式，或改用它实际可访问的宿主机地址。公网 HTTPS 证书在 1Panel 网站设置中配置；`.dev` 域名必须启用 HTTPS。

## 5. 更新、备份与排查

### Docker Hub 直拉镜像

Docker Hub 公共镜像已发布后，可把仓库中的 `deploy.hub.env.example` 上传服务器并复制为 `deploy.env`。它指定固定版本标签；填写访问密码哈希后即可直拉：

```dotenv
BACKEND_IMAGE=macrohome/verda-backend:2026-09-17-1
WEB_IMAGE=macrohome/verda-web:2026-09-17-1
```

服务器只需上传 `compose.yaml`、`deploy.hub.env.example`、`backend/.env.example`。从项目根目录的 PowerShell 执行（替换 SSH 用户和 IP）：

```powershell
ssh USER@SERVER_IP "mkdir -p ~/verda/backend"
scp .\compose.yaml .\deploy.hub.env.example USER@SERVER_IP:~/verda/
scp .\backend\.env.example USER@SERVER_IP:~/verda/backend/
```

登录服务器后创建 `backend/.env`、`deploy.env`，填写 Key、访问密码哈希和域名或 SSH 隧道模式：

```bash
cd ~/verda
cp backend/.env.example backend/.env
cp deploy.hub.env.example deploy.env
chmod 600 backend/.env deploy.env
```

使用上文的 `caddy hash-password` 生成密码哈希并编辑两个配置文件，再运行：

```bash
docker compose --env-file deploy.env config --quiet
docker compose --env-file deploy.env pull backend web
docker compose --env-file deploy.env up -d --no-build
```

私有仓库需要先在服务器执行 `docker login`；公开仓库可直接拉取。使用固定 `TAG` 可以明确每次部署的版本，不建议在生产环境依赖会变化的 `latest`。

### 备份与排查

上传新版本并解压覆盖代码后，运行 `docker compose --env-file deploy.env up -d --build`。部署时避开正在运行的调研任务：现有 SSE 任务在后端进程内执行，重启会中断任务。`docker compose down` 保留数据卷；不要对有用数据执行 `down -v`。
修改服务器上的 `backend/.env` 后，运行 `docker compose --env-file deploy.env up -d --force-recreate backend`，让容器重新挂载 Secret；线上左下角配置弹窗只显示状态，不允许写入密钥。

使用 SQLite 在线备份 API 生成一致性备份并复制到服务器目录：

```bash
docker compose --env-file deploy.env exec -T backend python -c 'import sqlite3; s=sqlite3.connect("/data/verda.db"); d=sqlite3.connect("/data/verda-backup.db"); s.backup(d); d.close(); s.close()'
docker compose --env-file deploy.env cp backend:/data/verda-backup.db ./verda-backup.db
```

排查时先看 `docker compose --env-file deploy.env ps` 和 `docker compose --env-file deploy.env logs --tail=200 backend web`。新版健康检查只验证后端响应，缺少 API Key 时 Web 仍可启动并提示访客填写。旧镜像 `2026-09-17-1` 的故障排查见 [旧版后端健康检查排查步骤](./DOCKER_BACKEND_UNHEALTHY.md)。本项目目前是单机单后端方案，SQLite 和任务状态不支持直接横向扩容。

## 本地验收记录

在 Docker Engine 27.4.0、Compose 2.31.0 上完成镜像构建和独立临时项目启动。验证结果：未认证访问返回 401；认证后首页、前端深链接、`/health` 与 `/api/*` 正常；SSE 首个事件可经 Caddy 实时到达；写入的测试订阅在后端容器重启后仍存在；容器中手动密钥写入接口不可用；镜像不包含本地 `.env` 或 SQLite 文件。测试容器、测试数据卷与测试用 `deploy.env` 已清理。前端 lint、构建与 npm 审计通过，审计为 0 项。完整付费调研流水线未在容器内重复运行。
