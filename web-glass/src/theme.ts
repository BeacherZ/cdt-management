/**
 * 外观主题：存在后端设置里（light / dark / system），默认跟随系统。
 *
 * 这里只负责「把某个模式的最终结果写到 <html> 上」：
 * 样式表那边靠 `:root[data-theme='dark']` 整套换变量，不再用 media query，
 * 所以后端说浅色就是浅色，哪怕系统是深色。
 *
 * system 是唯一会跟着客户端走的值：前端把 prefers-color-scheme 解析成
 * light / dark 再写上去，切系统主题时由 App 里的监听重新算一次。
 */
import type { ThemeMode } from './types'

export type ResolvedTheme = 'light' | 'dark'

const THEME_COLOR: Record<ResolvedTheme, string> = {
  light: '#eceff3',
  dark: '#080b0f',
}

const MQ = '(prefers-color-scheme: dark)'

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.(MQ).matches ? 'dark' : 'light'
}

export function resolveTheme(mode: ThemeMode | undefined): ResolvedTheme {
  if (mode === 'dark') return 'dark'
  if (mode === 'light') return 'light'
  // 默认跟随系统：没配过、字段缺失、老版本后端都落到这里
  return systemTheme()
}

/** 把主题落到 DOM。会同步 <meta> 的 color-scheme / theme-color，滚动条才不会打架 */
export function applyTheme(mode: ThemeMode | undefined): ResolvedTheme {
  const resolved = resolveTheme(mode)
  const root = document.documentElement
  if (resolved === 'dark') {
    root.dataset.theme = 'dark'
  } else {
    delete root.dataset.theme
  }
  document
    .querySelector('meta[name="color-scheme"]')
    ?.setAttribute('content', resolved)
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', THEME_COLOR[resolved])
  // 记下上次真正呈现的主题：index.html 里的内联脚本靠它在本轮请求返回前
  // 就把 data-theme 写上，深色用户刷新时不会先白一屏。
  try {
    localStorage.setItem('cdt_theme', resolved)
  } catch {
    /* 隐私模式下 localStorage 可能不可写，忽略 */
  }
  return resolved
}

/** 订阅系统深浅变化；返回取消订阅函数 */
export function watchSystemTheme(handler: (theme: ResolvedTheme) => void): () => void {
  const mq = window.matchMedia?.(MQ)
  if (!mq) return () => {}
  const listener = (event: MediaQueryListEvent) => handler(event.matches ? 'dark' : 'light')
  mq.addEventListener('change', listener)
  return () => mq.removeEventListener('change', listener)
}
