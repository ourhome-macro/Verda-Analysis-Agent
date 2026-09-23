import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { KeyRound, X } from 'lucide-react'
import { saveLLMConfig } from '../lib/api'
import type { LLMConfig, SaveLLMConfigBody } from '../lib/api'
import { clearBrowserCredentials, saveBrowserCredentials } from '../lib/browserCredentials'

interface Props {
  config: LLMConfig | null
  onClose: () => void
  onSaved: (config: LLMConfig) => void
}

const inputClass = 'mt-1.5 h-10 w-full rounded-btn border border-line bg-bg px-3 text-aux text-ink outline-none focus:border-primary'

export default function VApiSettingsDialog({ config, onClose, onSaved }: Props) {
  const [provider, setProvider] = useState<SaveLLMConfigBody['provider']>(
    config?.provider === 'zhipu' || config?.provider === 'custom' ? config.provider : 'deepseek',
  )
  const [apiKey, setApiKey] = useState('')
  const [bochaKey, setBochaKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState(config?.provider === 'custom' ? config.model : '')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const firstField = useRef<HTMLSelectElement>(null)

  useEffect(() => {
    firstField.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !saving) onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, saving])

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!config?.editable || saving) return
    setSaving(true)
    setMessage('')
    setError('')
    try {
      const updated = config.client_keys_required
        ? (() => {
            const keys = saveBrowserCredentials(apiKey, bochaKey)
            return { ...config, configured: Boolean(keys.deepseekApiKey),
              search_configured: Boolean(keys.bochaApiKey) }
          })()
        : await saveLLMConfig({
            provider,
            api_key: apiKey.trim(),
            bocha_api_key: bochaKey.trim(),
            base_url: provider === 'custom' ? baseUrl.trim() : '',
            model: provider === 'custom' ? model.trim() : '',
          })
      setApiKey('')
      setBochaKey('')
      setMessage(updated.configured && updated.search_configured
        ? '已保存，此浏览器可以开始调研。' : '已保存；补齐两项 Key 后即可开始调研。')
      onSaved(updated)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存配置失败')
    } finally {
      setSaving(false)
    }
  }

  const sameProvider = config?.provider === provider
  const modelKeyStatus = sameProvider && config?.configured ? '已填写，留空保持不变' : '粘贴 API Key'
  const browserMode = Boolean(config?.client_keys_required)

  function clearBrowserKeys() {
    if (!config) return
    clearBrowserCredentials()
    setApiKey('')
    setBochaKey('')
    setMessage('此浏览器保存的 API Key 已清除。')
    onSaved({ ...config, configured: false, search_configured: false })
  }
  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-ink/45 p-4"
      onMouseDown={(event) => { if (event.target === event.currentTarget && !saving) onClose() }}>
      <div role="dialog" aria-modal="true" aria-labelledby="api-settings-title"
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-card bg-card p-6 shadow-float">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <span className="grid h-10 w-10 place-items-center rounded-btn bg-primary-tint text-primary-deep">
              <KeyRound size={20} />
            </span>
            <div>
              <h2 id="api-settings-title" className="text-lg font-semibold text-ink">模型与搜索配置</h2>
              <p className="text-tag text-ink-3">{browserMode
                ? '密钥保存在当前浏览器，关闭后仍保留；页面不会回显'
                : '密钥仅保存在本机后端，页面不会回显'}</p>
            </div>
          </div>
          <button type="button" onClick={onClose} disabled={saving} aria-label="关闭配置"
            className="grid h-8 w-8 place-items-center rounded-btn text-ink-3 hover:bg-primary-tint disabled:opacity-50">
            <X size={18} />
          </button>
        </div>

        <div className="mt-5 flex flex-wrap gap-2 text-tag">
          <span className={`rounded-chip px-2.5 py-1 ${config?.configured ? 'bg-ok/15 text-ok' : 'bg-risk/10 text-risk'}`}>
            模型 Key：{config?.configured ? '已填写' : '未填写'}
          </span>
          <span className={`rounded-chip px-2.5 py-1 ${config?.search_configured ? 'bg-ok/15 text-ok' : 'bg-risk/10 text-risk'}`}>
            博查 Key：{config?.search_configured ? '已填写' : '未填写'}
          </span>
        </div>

        <form onSubmit={save} className="mt-5 space-y-4">
          <label className="block text-aux font-medium text-ink-2">
            模型服务
            <select ref={firstField} value={provider} disabled={!config?.editable || saving || browserMode}
              onChange={(event) => { setProvider(event.target.value as SaveLLMConfigBody['provider']); setError(''); setMessage('') }}
              className={inputClass}>
              <option value="deepseek">DeepSeek</option>
              {!browserMode && <option value="zhipu">智谱 GLM</option>}
              {!browserMode && <option value="custom">自定义 OpenAI 兼容接口</option>}
            </select>
          </label>
          {browserMode && <p className="text-tag text-ink-3">DeepSeek 接口地址：https://api.deepseek.com</p>}
          <label className="block text-aux font-medium text-ink-2">
            {provider === 'deepseek' ? 'DeepSeek API Key' : provider === 'zhipu' ? '智谱 API Key' : '自定义模型 API Key'}
            <input type="password" autoComplete="new-password" spellCheck={false} value={apiKey}
              onChange={(event) => setApiKey(event.target.value)} placeholder={modelKeyStatus}
              disabled={!config?.editable || saving} className={inputClass} />
          </label>
          {provider === 'custom' && !browserMode && (
            <>
              <label className="block text-aux font-medium text-ink-2">
                OpenAI 兼容网关地址
                <input type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder={sameProvider ? '留空保持现有地址' : 'https://example.com/v1'}
                  disabled={!config?.editable || saving} className={inputClass} />
              </label>
              <label className="block text-aux font-medium text-ink-2">
                默认模型名
                <input type="text" value={model} onChange={(event) => setModel(event.target.value)}
                  placeholder="模型 ID" disabled={!config?.editable || saving} className={inputClass} />
              </label>
            </>
          )}
          <label className="block text-aux font-medium text-ink-2">
            博查 Bocha API Key
            <input type="password" autoComplete="new-password" spellCheck={false} value={bochaKey}
              onChange={(event) => setBochaKey(event.target.value)}
              placeholder={config?.search_configured ? '已填写，留空保持不变' : '粘贴博查 API Key'}
              disabled={!config?.editable || saving} className={inputClass} />
          </label>

          {!config && <p role="alert" className="text-aux text-risk">无法连接后端，请确认服务已启动。</p>}
          {config && !config.editable && <p className="text-aux text-ink-3">当前环境仅可查看状态；本机编辑需启用 LOCAL_SETTINGS_ENABLED。</p>}
          {error && <p role="alert" className="text-aux text-risk">{error}</p>}
          {message && <p role="status" className="text-aux text-ok">{message}</p>}
          <div className="flex justify-end gap-2 pt-1">
            {browserMode && <button type="button" onClick={clearBrowserKeys} disabled={saving}
              className="mr-auto h-10 px-2 text-aux text-risk hover:underline disabled:opacity-50">
              清除此浏览器的 Key
            </button>}
            <button type="button" onClick={onClose} disabled={saving}
              className="h-10 rounded-btn border border-line px-4 text-aux text-ink-2 hover:bg-bg disabled:opacity-50">
              关闭
            </button>
            <button type="submit" disabled={!config?.editable || saving}
              className="h-10 rounded-btn bg-primary px-5 text-aux font-medium text-white hover:bg-primary-deep disabled:opacity-50">
              {saving ? '保存中…' : '保存配置'}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}
