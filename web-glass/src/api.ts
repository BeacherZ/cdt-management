export class APIError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

/** 普通请求的超时。后端在本机/内网，20 秒已经非常宽松 */
const DEFAULT_TIMEOUT_MS = 20_000
/**
 * 慢接口的超时：新建换班组要等引擎锁（最多 30 秒）再走完一轮下发；
 * 手动换班、全量 DNS 同步、费用查询都要连打好几次阿里云 / Cloudflare。
 */
export const SLOW_TIMEOUT_MS = 90_000

type ApiInit = RequestInit & { timeoutMs?: number }

function cookie(name: string) {
  const prefix = `${name}=`
  // split(';') 后再 trim：规范输出是 "; "，但代理/老浏览器可能只给 ";"，
  // 只按 "; " 切会漏掉 CSRF，写操作全部被 403 挡回来
  const value = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
  return value ? decodeURIComponent(value.slice(prefix.length)) : ''
}

export async function api<T>(path: string, init: ApiInit = {}): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...rest } = init
  const headers = new Headers(rest.headers)
  if (rest.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (rest.method && rest.method !== 'GET' && rest.method !== 'HEAD') {
    const csrf = cookie('cdt_mgmt_csrf')
    if (csrf) headers.set('X-CDT-CSRF', csrf)
  }

  // 调用方自己带 signal（比如组件卸载取消）时以它为准，内部不再叠加超时
  const controller = rest.signal ? null : new AbortController()
  const timer = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : 0

  let response: Response
  try {
    response = await fetch(path, {
      ...rest,
      headers,
      credentials: 'same-origin',
      signal: rest.signal ?? controller?.signal,
    })
  } catch (error) {
    // fetch 只在网络层失败时 reject。原样抛出去，界面上就是一句英文
    // 「Failed to fetch」，用户既看不出是断网还是后端挂了，也不知道该等还是该重启。
    if (controller?.signal.aborted) {
      throw new APIError(
        0,
        'timeout',
        `请求超时（${Math.round(timeoutMs / 1000)} 秒未响应），后端可能正忙，稍后再试`,
      )
    }
    throw new APIError(0, 'network', '连不上后端，请确认服务正在运行、网络通畅')
  } finally {
    if (timer) window.clearTimeout(timer)
  }

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

export function post<T>(path: string, payload?: unknown, timeoutMs?: number): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    body: payload === undefined ? undefined : JSON.stringify(payload),
    timeoutMs,
  })
}

export function del<T>(path: string, timeoutMs?: number): Promise<T> {
  return api<T>(path, { method: 'DELETE', timeoutMs })
}

export async function fetchLatestRelease(repo: string): Promise<string> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 5_000)
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
    window.clearTimeout(timer)
  }
}
