/** An unguessable browser-owned workspace token; it is never placed in URLs. */
const STORAGE_KEY = 'verda:visitor-token:v1'
const TOKEN_PATTERN = /^[0-9a-f]{64}$/

export function visitorHeaders(): Record<string, string> {
  let token = localStorage.getItem(STORAGE_KEY) || ''
  if (!TOKEN_PATTERN.test(token)) {
    const bytes = crypto.getRandomValues(new Uint8Array(32))
    token = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
    localStorage.setItem(STORAGE_KEY, token)
  }
  return { 'X-Verda-Visitor': token }
}
