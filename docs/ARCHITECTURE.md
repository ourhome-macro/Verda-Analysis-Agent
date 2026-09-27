# 架构与数据流

## 运行组件

| 组件 | 入口 | 职责 |
|---|---|---|
| React 前端 | `frontend/src` | 澄清、进度流、证据与矩阵展示、报告阅读 |
| FastAPI 后端 | `backend/app/main.py` | 任务接口、SSE、报告与本地持久化 |
| Vercel 入口 | `api/index.py` | 与 `backend/app/core` 对应的部署副本 |
| SQLite | `backend/app/core/db.py` | 任务、报告、证据、Trace 与运行事件 |

Docker Compose 使用一个后端 worker。SQLite 任务租约和进程内状态目前按单机部署设计；多实例部署需要共享任务、限流和数据存储。

## 研究流水线

```text
创建任务 → 领域/竞品候选 → 用户澄清 → 研究契约
       → 专家指派 → 搜索与正文抓取 → 品牌×维度 Claim 提取
       → 原文引句核验 → 规则质检 + 模型审阅
       → 定向 Replan / 补采 / 重分析（至多 2 轮）
       → 章节写作 → 最终审校 → 报告
```

研究契约固定品牌、维度、目标市场、研究截止日和时效窗口。证据采集保存 URL、正文、来源类别和发布日期；Claim 只可引用当次模型可见的证据片段。矩阵分别计算“有原文支持的事实”和“时效窗口内可证明的事实”。审校后的报告可能标记为 `needs_review`，任务运行完成不代表研究质量通过。

关键实现位于 `backend/app/core/orchestrator.py`、`research_contract.py`、`research_planner.py`、`claim_verifier.py`、`audit.py` 与 `final_audit.py`。`api/app/core` 是部署镜像，变更时须保持一致。

## Trace 的范围

Trace 记录阶段、成功的模型调用、耗时、Token 和人工汇总动作；报告另存证据快照、Claim、矩阵和质检结果。模型输入输出仅保留摘要，成功搜索与单次网页抓取没有完整的逐请求 Trace，因此它不能独立用于精确重放所有网络动作。有关数据集回流的前提见 [可观测性与反馈](TRACE_AND_FEEDBACK.md)。
