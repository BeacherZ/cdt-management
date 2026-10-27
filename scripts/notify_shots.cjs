/* 把 test-data/notify/*.html 截成 README 用的通知示例图。

   前置：先跑 python scripts/notify_shots.py 生成 HTML
   用法：node scripts/notify_shots.cjs
*/

const { spawn } = require('child_process')
const fs = require('fs')
const os = require('os')
const path = require('path')

const SRC = path.join(__dirname, '..', 'test-data', 'notify')
const OUT = path.join(__dirname, '..', 'docs', 'screenshots')
const PORT = 9334
const WIDTH = 612 // 卡片 560 + body 左右各 26 的留白
const PADDING = 26

const BROWSERS = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
]

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function findBrowser() {
  for (const candidate of BROWSERS) {
    if (fs.existsSync(candidate)) return candidate
  }
  throw new Error('没找到 Chrome / Edge，可用 CHROME_PATH 环境变量指定')
}

async function waitForDevtools(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/list`)
      const targets = await response.json()
      const page = targets.find((item) => item.type === 'page' && item.webSocketDebuggerUrl)
      if (page) return page.webSocketDebuggerUrl
    } catch (_error) {
      /* 还没起来，继续等 */
    }
    await sleep(300)
  }
  throw new Error('DevTools 端口没就绪')
}

class Cdp {
  constructor(socket) {
    this.socket = socket
    this.seq = 0
    this.pending = new Map()
    this.listeners = new Map()
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id)
        this.pending.delete(message.id)
        message.error ? reject(new Error(message.error.message)) : resolve(message.result)
        return
      }
      for (const handler of this.listeners.get(message.method) || []) handler(message.params)
    })
  }

  send(method, params = {}) {
    const id = ++this.seq
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  on(method, handler) {
    if (!this.listeners.has(method)) this.listeners.set(method, [])
    this.listeners.get(method).push(handler)
  }

  waitFor(method, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`等待 ${method} 超时`)), timeoutMs)
      this.on(method, (params) => {
        clearTimeout(timer)
        resolve(params)
      })
    })
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    })
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text)
    }
    return result.result.value
  }

  async goto(url) {
    const loaded = this.waitFor('Page.loadEventFired')
    await this.send('Page.navigate', { url })
    await loaded
  }

  async viewport(width, height, dsf = 2) {
    await this.send('Emulation.setDeviceMetricsOverride', {
      width,
      height,
      deviceScaleFactor: dsf,
      mobile: false,
    })
  }

  async shot(file) {
    const { data } = await this.send('Page.captureScreenshot', {
      format: 'png',
      fromSurface: true,
      captureBeyondViewport: true,
    })
    fs.writeFileSync(path.join(OUT, file), Buffer.from(data, 'base64'))
  }
}

;(async () => {
  if (!fs.existsSync(SRC)) {
    throw new Error(`没找到 ${SRC}，先跑 python scripts/notify_shots.py`)
  }
  fs.mkdirSync(OUT, { recursive: true })

  const pages = fs
    .readdirSync(SRC)
    .filter((name) => name.endsWith('.html'))
    .sort()
  if (!pages.length) throw new Error(`${SRC} 里没有 html`)

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cdt-notify-'))
  const browser = spawn(
    findBrowser(),
    [
      '--headless=new',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--hide-scrollbars',
      '--force-color-profile=srgb',
      '--allow-file-access-from-files',
      'about:blank',
    ],
    { stdio: 'ignore' },
  )

  let cdp
  try {
    const wsUrl = await waitForDevtools()
    const socket = new WebSocket(wsUrl)
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve)
      socket.addEventListener('error', reject)
    })
    cdp = new Cdp(socket)
    await cdp.send('Page.enable')
    await cdp.send('Runtime.enable')

    for (const name of pages) {
      const url = 'file:///' + path.join(SRC, name).replace(/\\/g, '/')
      await cdp.viewport(WIDTH, 900)
      await cdp.goto(url)
      await sleep(500)

      // 按卡片实际高度裁掉下方空白，图不带大片底色
      const cardHeight = await cdp.evaluate(
        `Math.ceil(document.querySelector('.card').getBoundingClientRect().height)`,
      )
      const height = cardHeight + PADDING * 2
      await cdp.viewport(WIDTH, height)
      await sleep(300)

      const png = name.replace(/\.html$/, '.png')
      await cdp.shot(png)
      console.log(`  ✓ ${png}  (${WIDTH}×${height})`)
    }
  } finally {
    if (cdp) cdp.socket.close()
    browser.kill()
  }
})().catch((error) => {
  console.error('截图失败：' + error.message)
  process.exit(1)
})
