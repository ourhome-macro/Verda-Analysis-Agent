# Verda Analysis Agent

Verda 是一个基于真实网页证据的竞品调研工作台。用户确认研究对象、竞品、维度和时效范围后，系统执行搜索与正文采集、逐条事实核验、矩阵质检、定向 Replan，并生成带来源引用的报告。

## 能力

- **研究契约**：把品牌、维度和研究截止日固定为品牌 × 维度矩阵；用户选择的竞品不会替代原始调研对象。
- **证据链**：博查搜索、网页抓取、来源准入、Claim 原文引句核验与最终报告审校。
- **有限返工**：按矩阵缺口修订目标单元的检索词并补采；快速、深度、专家模式分别允许 0、1、2 轮，始终不超过 2 轮。
- **出站保护**：单凭据、供应商和单进程全局的请求速率与在途并发限制，带超时、有限重试和日志。
- **可观测性**：SSE 进度、阶段与模型摘要 Trace、证据快照和报告质量状态。Trace 尚不提供逐次搜索/抓取请求的完整回放。

## 本地运行

建议使用 Python 3.11 与 Node.js 22。PowerShell 示例：

```powershell
Copy-Item backend/.env.example backend/.env
python -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r requirements.txt 'uvicorn[standard]==0.32.1'
```

在 `backend/.env` 设置模型和搜索凭据后，分别启动两个终端：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8010
```

```powershell
Set-Location frontend
npm ci
npm run dev
```

前端默认位于 `http://localhost:3400`，开发代理将 `/api` 转到 `http://127.0.0.1:8010`。不要将 `.env` 或浏览器密钥提交到仓库。

## 文档

| 主题 | 文档 |
|---|---|
| 架构与数据流 | [架构](docs/ARCHITECTURE.md) |
| 配置与部署 | [部署配置](docs/DEPLOYMENT.md) |
| Plan、Replan 与限流 | [调研流程](docs/RESEARCH_WORKFLOW.md) |
| Trace、Bad Case 与数据回流边界 | [可观测性与反馈](docs/TRACE_AND_FEEDBACK.md) |
| 本次独立发布的文件范围 | [发布记录](docs/RELEASE_NOTES.md) |
| 早期项目来源与许可 | [来源说明](docs/PROVENANCE.md) |

本仓库采用 [AGPL-3.0](LICENSE)。
