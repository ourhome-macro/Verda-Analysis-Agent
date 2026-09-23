/** Visitor-owned keys stay in this browser and are only sent to API calls that need them. */
export const BROWSER_CREDENTIALS_KEY = 'verda:browser-api-credentials:v1'

export interface BrowserCredentials {
  deepseekApiKey: string
  bochaApiKey: string
}

const EMPTY: BrowserCredentials = { deepseekApiKey: '', bochaApiKey: '' }
const KEY_PATTERN = /^[A-Za-z0-9._~+/=:-]{1,2048}$/

export function readBrowserCredentials(): BrowserCredentials {
  try {
    const data = JSON.parse(localStorage.getItem(BROWSER_CREDENTIALS_KEY) || '{}') as Partial<BrowserCredentials>
    return {
      deepseekApiKey: typeof data.deepseekApiKey === 'string' ? data.deepseekApiKey : '',
      bochaApiKey: typeof data.bochaApiKey === 'string' ? data.bochaApiKey : '',
    }
  } catch {
    return EMPTY
  }
}

export function saveBrowserCredentials(deepseekApiKey: string, bochaApiKey: string): BrowserCredentials {
  const previous = readBrowserCredentials()
  const next = {
    deepseekApiKey: deepseekApiKey.trim() || previous.deepseekApiKey,
    bochaApiKey: bochaApiKey.trim() || previous.bochaApiKey,
  }
  if ((next.deepseekApiKey && !KEY_PATTERN.test(next.deepseekApiKey)) ||
      (next.bochaApiKey && !KEY_PATTERN.test(next.bochaApiKey))) {
    throw new Error('API Key 格式不正确，请检查空格或特殊字符')
  }
  localStorage.setItem(BROWSER_CREDENTIALS_KEY, JSON.stringify(next))
  return next
}

export function clearBrowserCredentials(): void {
  localStorage.removeItem(BROWSER_CREDENTIALS_KEY)
}

export function browserCredentialHeaders(): Record<string, string> {
  const credentials = readBrowserCredentials()
  if (!credentials.deepseekApiKey || !credentials.bochaApiKey) return {}
  return {
    'X-Verda-Deepseek-Key': credentials.deepseekApiKey,
    'X-Verda-Bocha-Key': credentials.bochaApiKey,
  }
}
