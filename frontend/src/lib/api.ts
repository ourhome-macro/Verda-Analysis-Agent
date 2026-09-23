import type {
  CreateTaskResp,
  DashboardStats,
  EvidenceQueryResp,
  Expert,
  ExpertWorkload,
  Report,
  ReportCard,
  ReportSection,
  SSEEventType,
  Subscription,
  TraceSpan,
} from '../types'
import { browserCredentialHeaders, readBrowserCredentials } from './browserCredentials'
import { visitorHeaders } from './visitorIdentity'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function safeJson<T>(path: string, init?: RequestInit, fallback?: T): Promise<T> {
  try {
    const headers = new Headers(init?.headers)
    for (const [name, value] of Object.entries(visitorHeaders())) headers.set(name, value)
    const r = await fetch(`${API_BASE}${path}`, { ...init, headers })
    if (!r.ok) {
      const body = await r.json().catch(() => null) as { detail?: string } | null
      throw new Error(body?.detail || `HTTP ${r.status}`)
    }
    return (await r.json()) as T
  } catch (e) {
    if (fallback !== undefined) return fallback
    throw e
  }
}

/* 48 专家：优先后端，失败回退本地 JSON（绝不白屏） */
export async function fetchExperts(): Promise<Expert[]> {
  try {
    const r = await fetch(`${API_BASE}/api/experts`, { headers: visitorHeaders() })
    if (r.ok) {
      const data = await r.json()
      if (Array.isArray(data) && data.length) return data
      if (data?.experts?.length) return data.experts
    }
  } catch {
    /* fall through */
  }
  const local = await fetch('/assets/experts.json')
  return (await local.json()) as Expert[]
}

export async function createTask(query: string, mode: string = 'deep'): Promise<CreateTaskResp> {
  return safeJson<CreateTaskResp>(
    '/api/tasks',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...browserCredentialHeaders() },
      body: JSON.stringify({ query, mode }),
    },
  )
}

export async function submitClarify(
  taskId: string,
  answers: Record<string, unknown>,
): Promise<{ ok: boolean }> {
  return safeJson(
    `/api/tasks/${taskId}/clarify`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    },
  )
}

export interface LLMConfig {
  provider: string
  model: string
  core_model: string
  aux_model: string
  fast_model: string
  configured: boolean
  search_configured: boolean
  editable: boolean
  client_keys_required: boolean
}

export const LLM_CONFIG_UPDATED_EVENT = 'verda:llm-config-updated'
export const OPEN_API_SETTINGS_EVENT = 'verda:open-api-settings'

export async function fetchLLMConfig(): Promise<LLMConfig | null> {
  const server = await safeJson<LLMConfig | null>('/api/llm/config', { cache: 'no-store' }, null)
  if (!server?.client_keys_required) return server
  const credentials = readBrowserCredentials()
  return {
    ...server,
    provider: 'deepseek',
    model: 'deepseek-flash',
    core_model: 'deepseek-v4-pro',
    aux_model: 'deepseek-flash',
    fast_model: 'deepseek-flash',
    configured: Boolean(credentials.deepseekApiKey),
    search_configured: Boolean(credentials.bochaApiKey),
    editable: true,
  }
}

export interface SaveLLMConfigBody {
  provider: 'deepseek' | 'zhipu' | 'custom'
  api_key?: string
  bocha_api_key?: string
  base_url?: string
  model?: string
}

export async function saveLLMConfig(body: SaveLLMConfigBody): Promise<LLMConfig> {
  return safeJson<LLMConfig>('/api/llm/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  })
}

export async function fetchReport(reportId: string): Promise<Report | null> {
  return safeJson<Report | null>(`/api/reports/${reportId}`, undefined, null)
}

/* 报告决策链路 Trace（决策回放 / Trace 页签） */
export async function fetchReportTrace(reportId: string): Promise<{ spans: TraceSpan[] }> {
  return safeJson<{ spans: TraceSpan[] }>(`/api/reports/${reportId}/trace`, undefined, { spans: [] })
}

/* 人工反馈（修正率 → 业务闭环指标） */
export async function submitFeedback(
  reportId: string,
  editedBlocks: number,
  totalBlocks: number,
  data: Record<string, unknown> = {},
): Promise<{ ok: boolean }> {
  return safeJson(
    `/api/reports/${reportId}/feedback`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_blocks: editedBlocks, total_blocks: totalBlocks, data }),
    },
    { ok: true },
  )
}

/* 按批注深化章节（人工介入二次调研） */
export async function refineSection(
  reportId: string,
  sectionId: string,
  annotations: string[],
): Promise<{ ok: boolean; section?: ReportSection; message?: string }> {
  return safeJson(
    `/api/reports/${reportId}/refine`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...browserCredentialHeaders() },
      body: JSON.stringify({ section_id: sectionId, annotations }),
    },
    { ok: false, message: '请求失败' },
  )
}

/* 我的调研：真实历史报告列表 */
export async function fetchReports(): Promise<ReportCard[]> {
  return safeJson<ReportCard[]>('/api/reports', undefined, [])
}

/* 仪表盘真实统计 */
export async function fetchDashboard(): Promise<DashboardStats | null> {
  return safeJson<DashboardStats | null>('/api/dashboard', undefined, null)
}

/* 全局证据溯源库 */
export async function fetchEvidences(params?: {
  brand?: string
  source_type?: string
  min_cred?: number
}): Promise<EvidenceQueryResp> {
  const qs = new URLSearchParams()
  if (params?.brand) qs.set('brand', params.brand)
  if (params?.source_type) qs.set('source_type', params.source_type)
  if (params?.min_cred != null) qs.set('min_cred', String(params.min_cred))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return safeJson<EvidenceQueryResp>(`/api/evidences${suffix}`, undefined, {
    items: [],
    facets: { total: 0, by_type: {}, by_brand: {} },
  })
}

/* 竞品监控订阅 */
export async function fetchSubscriptions(): Promise<Subscription[]> {
  return safeJson<Subscription[]>('/api/subscriptions', undefined, [])
}

export async function createSubscription(query: string, brands: string[]): Promise<Subscription | null> {
  return safeJson<Subscription | null>(
    '/api/subscriptions',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, brands }),
    },
    null,
  )
}

export async function deleteSubscription(subId: string): Promise<{ ok: boolean }> {
  return safeJson(`/api/subscriptions/${subId}`, { method: 'DELETE' }, { ok: true })
}

/* 专家工作量看板 */
export async function fetchWorkload(): Promise<ExpertWorkload[]> {
  return safeJson<ExpertWorkload[]>('/api/experts/workload', undefined, [])
}

/* SSE：监听任务流，返回关闭函数 */
export interface SSEHandlers {
  onEvent: (type: SSEEventType, data: unknown) => void
  onError?: (e: unknown) => void
  onOpen?: () => void
}

export function openTaskStream(taskId: string, handlers: SSEHandlers): () => void {
  const url = `${API_BASE}/api/tasks/${taskId}/stream`
  const controller = new AbortController()
  let closed = false
  let completed = false
  let cursor = 0
  const types: SSEEventType[] = [
    'node_update',
    'thought',
    'message',
    'evidence',
    'chart',
    'image',
    'progress',
    'trace',
    'report_ready',
    'done',
    'error',
  ]
  function handleBlock(block: string) {
    let eventType = ''
    let eventId = 0
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('id:')) eventId = Number(line.slice(3).trim()) || 0
      if (line.startsWith('event:')) eventType = line.slice(6).trim()
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (!types.includes(eventType as SSEEventType)) return
    if (eventId && eventId <= cursor) return
    let parsed: unknown = data.join('\n')
    try {
      parsed = JSON.parse(data.join('\n'))
    } catch {
      /* keep raw text */
    }
    if (eventType === 'done' || eventType === 'error') completed = true
    handlers.onEvent(eventType as SSEEventType, parsed)
    if (eventId) cursor = eventId
  }

  void (async () => {
    for (let attempt = 0; attempt < 4 && !closed && !completed; attempt++) {
      try {
        const response = await fetch(`${url}?after=${cursor}`, {
          headers: { ...visitorHeaders(), ...browserCredentialHeaders() },
          cache: 'no-store', signal: controller.signal,
        })
        if (!response.ok) {
          const body = await response.json().catch(() => null) as { detail?: string } | null
          throw new Error(body?.detail || `HTTP ${response.status}`)
        }
        if (!response.body) throw new Error('浏览器未提供任务流')
        handlers.onOpen?.()
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (!closed && !completed) {
          const { done, value } = await reader.read()
          if (done) break
          buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
          let boundary = buffer.indexOf('\n\n')
          while (boundary >= 0) {
            handleBlock(buffer.slice(0, boundary))
            buffer = buffer.slice(boundary + 2)
            boundary = buffer.indexOf('\n\n')
          }
        }
        await reader.cancel().catch(() => {})
        if (closed || completed) return
        throw new Error('连接暂时中断，任务仍在后台执行')
      } catch (error) {
        if (closed || completed) return
        if (attempt < 3) {
          await new Promise((resolve) => setTimeout(resolve, 1000 * (attempt + 1)))
          continue
        }
        handlers.onError?.(error)
        handlers.onEvent('error', { message: '连接恢复失败；后台任务继续执行，可刷新工作台恢复观察' })
      }
    }
  })()
  return () => {
    closed = true
    controller.abort()
  }
}

export { API_BASE }
