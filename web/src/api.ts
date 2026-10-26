export class APIError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

function cookie(name: string) {
  const prefix = `${name}=`
  const value = document.cookie.split('; ').find((part) => part.startsWith(prefix))
  return value ? decodeURIComponent(value.slice(prefix.length)) : ''
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (init.method && init.method !== 'GET' && init.method !== 'HEAD') {
    const csrf = cookie('cdt_mgmt_csrf')
    if (csrf) headers.set('X-CDT-CSRF', csrf)
  }
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  if (response.status === 204) return undefined as T
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body?.error
    throw new APIError(
      response.status,
      detail?.code ?? 'request_failed',
      detail?.message ?? `HTTP ${response.status}`,
    )
  }
  return body as T
}

export function post<T>(path: string, payload?: unknown): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    body: payload === undefined ? undefined : JSON.stringify(payload),
  })
}

export function del<T>(path: string): Promise<T> {
  return api<T>(path, { method: 'DELETE' })
}

export async function fetchLatestRelease(repo: string): Promise<string> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 5_000)
  try {
    const response = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, {
      headers: { Accept: 'application/vnd.github+json' },
      credentials: 'omit',
      cache: 'no-store',
      signal: controller.signal,
    })
    const payload = (await response.json().catch(() => null)) as { tag_name?: unknown } | null
    if (!response.ok || !payload || typeof payload.tag_name !== 'string') {
      throw new Error('无法获取最新版本')
    }
    return payload.tag_name
  } finally {
    window.clearTimeout(timeout)
  }
}
