# 本机模型与搜索配置（2026-09-17）

左下角「林研究员 / 青野科技」现在是可点击的配置入口，可选择 DeepSeek、智谱或自定义 OpenAI 兼容模型服务，并填写模型 API Key 与博查 API Key。自定义服务还需填写 HTTPS 网关和默认模型名。

## 启用

只在本机开发时于未跟踪的 `backend/.env` 设置 `LOCAL_SETTINGS_ENABLED=true`，并让 Vite 与 FastAPI 监听 `127.0.0.1`。默认值为 `false`；Vercel / Lambda 环境始终拒绝写入。弹窗中的空白 Key 表示保留现有值，已保存的 Key 不会返回给前端。保存后后端清除配置缓存并重建 LLM 客户端，后续请求立即使用新配置；调研流运行中禁止修改。

后端会校验本机连接和浏览器 Origin，拒绝跨站写入；`.env` 在同目录临时文件中写完后原子替换，残留的 `.env-*.tmp` 也被 Git 忽略。环境变量优先级高于 `.env`，如果某项已由进程环境变量提供，页面不能覆盖它。

## 代码位置

- `backend/app/core/local_settings.py`：输入校验、访问限制与 `.env` 原子写入。
- `backend/app/main.py`：`GET/PUT /api/llm/config`，仅返回 Provider、模型和配置状态。
- `frontend/src/components/VApiSettingsDialog.tsx`：本机配置弹窗。
- `frontend/src/layout/VSidebar.tsx`：左下角入口。
- `api/` 保持与本地后端逻辑一致；云端写入被禁用。

## 验证

已通过后端临时 `.env` 的保存、切换 Provider、空值保留和非法输入测试；前端 ESLint、TypeScript 与 Vite 构建通过。本机经 Vite 代理读取配置、拒绝跨站来源、拒绝注入式 Key、留空保存与热更新均通过，且保存前后现有 DeepSeek、博查 Key 保持一致；保存后真实 DeepSeek ping 成功。Vercel 镜像的写入接口返回 403。未在浏览器自动化环境中执行视觉点击测试。
