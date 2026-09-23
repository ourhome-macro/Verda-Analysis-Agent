import { useEffect, useState } from 'react'
import { KeyRound } from 'lucide-react'
import { fetchLLMConfig, LLM_CONFIG_UPDATED_EVENT, OPEN_API_SETTINGS_EVENT } from '../lib/api'
import type { LLMConfig } from '../lib/api'

export default function VApiKeyBanner() {
  const [config, setConfig] = useState<LLMConfig | null>(null)

  useEffect(() => {
    const refresh = () => { void fetchLLMConfig().then(setConfig) }
    refresh()
    window.addEventListener(LLM_CONFIG_UPDATED_EVENT, refresh)
    return () => window.removeEventListener(LLM_CONFIG_UPDATED_EVENT, refresh)
  }, [])

  if (!config?.client_keys_required || (config.configured && config.search_configured)) return null
  const missing = [!config.configured && 'DeepSeek API Key', !config.search_configured && '博查 API Key']
    .filter(Boolean).join('和')

  return (
    <div role="status" className="flex min-h-11 items-center justify-center gap-3 border-b border-warn/25 bg-sun-soft px-4 py-2 text-aux text-ink-2">
      <KeyRound size={16} className="shrink-0 text-warn" />
      <span>开始调研前，请填写此浏览器的{missing}。</span>
      <button type="button" onClick={() => window.dispatchEvent(new Event(OPEN_API_SETTINGS_EVENT))}
        className="shrink-0 font-semibold text-primary-deep underline underline-offset-2 hover:text-primary">
        现在填写
      </button>
    </div>
  )
}
