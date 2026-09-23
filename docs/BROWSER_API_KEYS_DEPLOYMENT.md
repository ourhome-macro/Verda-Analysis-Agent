# 访客自行填写 API Key 的部署方案

## 行为

新版 Compose 为后端设置 `REQUIRE_CLIENT_API_KEYS=true`。后端只需能够启动并响应 `/health` 就能让 Web 上线；`backend/.env` 仍需作为 Compose Secret 文件存在，但模型与博查 Key 可以留空。默认模型服务为 DeepSeek，访客请求固定使用 `https://api.deepseek.com`；搜索固定使用博查官方接口。DeepSeek 当前官方模型名为 `deepseek-flash` 和 `deepseek-v4-pro`。

首页上方在缺少任一 Key 时显示通知栏。访客从通知栏或左下角打开配置弹窗，填写自己的 DeepSeek Key 和博查 Key。两项值写入当前浏览器的 `localStorage`，关闭浏览器后仍保留；页面不回显原值，可在弹窗中清除。创建任务和执行流式调研时，浏览器仅向同源 `/api` 请求的 Header 发送两项 Key。后端对每条请求建立独立配置和 LLM 客户端，任务结束即关闭客户端，不把访客 Key 写进 SQLite、服务器 `.env`、响应或 URL。缺少任一 Key 的访客不能开始调研。

长期保存在浏览器中的 Key 可被同一浏览器配置文件下的其他使用者或站点脚本读取，因此仅适合个人浏览器与可信站点代码。生产入口必须使用 HTTPS；1Panel 代理到本机 `127.0.0.1:8088` 的链路仍可用 HTTP。现有 Basic Auth 只是一套全站登录，报告、历史记录和订阅仍在同一 SQLite 中共享；此次变更只隔离 API Key 和模型调用费用，不提供完整的多租户数据隔离。

## 版本与上线

`macrohome/verda-backend:2026-09-17-1` 和同标签 Web 是旧镜像，不包含浏览器 Key 流程。**必须同时构建或发布本次修改后的后端与 Web 镜像，并在 `deploy.env` 指向新标签；仅上传新版 `compose.yaml` 并执行 `--no-build` 不会得到新功能。** 保留旧标签可用于回滚。

若服务器上有完整源码，可在维护窗口执行：

```bash
cd /opt/verda
docker compose --env-file deploy.env config --quiet
docker compose --env-file deploy.env up -d --build --force-recreate backend web
docker compose --env-file deploy.env ps
curl -I http://127.0.0.1:8088/
```

若服务器只拉 Docker Hub 镜像，先在构建机发布新的前后端标签，再修改服务器 `deploy.env` 中的 `BACKEND_IMAGE` 与 `WEB_IMAGE`，执行 `docker compose --env-file deploy.env pull backend web` 和 `docker compose --env-file deploy.env up -d --no-build --force-recreate backend web`。不要在真实调研任务运行时重建后端，重启会中断进程内任务。

## 验收

1. 服务器的 `backend/.env` 不填模型与博查 Key。Compose `ps` 应显示 `backend (healthy)` 与 `web` 运行，未登录的 `curl -I http://127.0.0.1:8088/` 应返回 401。
2. 登录网页后，通知栏应提示填写两项 Key；输入保存后通知栏消失，关闭并重新打开浏览器仍显示已填写状态。
3. 用两个不同浏览器分别填写不同 Key；各自创建调研任务时，后端只使用该请求的 Key。清除其中一个浏览器的 Key，不应影响另一个浏览器。
4. 以无 Key 的浏览器尝试创建任务应得到明确的配置错误；`/health` 仍正常，以便 Web 保持可访问。

本地已通过请求头校验、异步线程并发隔离、LLM 客户端隔离和流式接口回归测试；前端 TypeScript、ESLint 与生产构建通过。另用两张新镜像在隔离 Compose 项目中、空 API Key 文件下验证：后端与 Web 均为 `healthy`，未认证访问返回 401，配置接口显示访客模式开启且两项未配置，无请求头创建任务返回 400，携带两项测试 Key 创建任务返回 200。测试容器、网络与数据卷已清理。外部 DeepSeek/博查付费接口未在本次修改中调用。
