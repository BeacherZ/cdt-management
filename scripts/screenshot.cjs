/* UI 截图：直接走 Chrome DevTools Protocol，驱动本机 Edge/Chrome 无头模式。
   不依赖 playwright/puppeteer，任何装了 Edge 或 Chrome 的机器都能跑。

   前置：本地已用 dry-run 起服务并灌好演示数据
     CDT_DRY_RUN=1 CDT_DATA_DIR=./test-data ./.venv/Scripts/python.exe -m app.main
     python scripts/seed_demo.py

   用法：node scripts/screenshot.cjs [base_url]
*/

const { spawn } = require('child_process')
const fs = require('fs')
const os = require('os')
const path = require('path')

const BASE = process.argv[2] || 'http://127.0.0.1:8099'
const OUT = path.join(__dirname, '..', 'docs', 'screenshots')
const PORT = 9333
const VIEWPORT = { width: 1440, height: 980, dsf: 2 }
const MOBILE = { width: 420, height: 900, dsf: 2 }

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
      throw new Error(result.exceptionDetails.text + ' ' + (result.exceptionDetails.exception?.description || ''))
    }
    return result.result.value
  }

  async goto(url) {
    const loaded = this.waitFor('Page.loadEventFired')
    await this.send('Page.navigate', { url })
    await loaded
  }

  async viewport({ width, height, dsf }, mobile = false) {
    await this.send('Emulation.setDeviceMetricsOverride', {
      width,
      height,
      deviceScaleFactor: dsf,
      mobile,
    })
  }

  async shot(file) {
    await sleep(500)
    const { data } = await this.send('Page.captureScreenshot', { format: 'png', fromSurface: true })
    fs.writeFileSync(path.join(OUT, file), Buffer.from(data, 'base64'))
    console.log('  ✓ ' + file)
  }

  async waitSelector(selector, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      if (await this.evaluate(`!!document.querySelector(${JSON.stringify(selector)})`)) return
      await sleep(200)
    }
    throw new Error('等不到元素 ' + selector)
  }

  async clickByText(selector, text) {
    const ok = await this.evaluate(`(() => {
      const nodes = [...document.querySelectorAll(${JSON.stringify(selector)})]
      const target = nodes.find((node) => node.textContent.replace(/\\s+/g, '').includes(${JSON.stringify(
        text.replace(/\s+/g, ''),
      )}))
      if (!target) return false
      target.click()
      return true
    })()`)
    if (!ok) throw new Error(`点不到 ${selector} 里的「${text}」`)
    await sleep(600)
  }

  async clickSelector(selector) {
    const ok = await this.evaluate(`(() => {
      const node = document.querySelector(${JSON.stringify(selector)})
      if (!node) return false
      node.click()
      return true
    })()`)
    if (!ok) throw new Error('点不到 ' + selector)
    await sleep(600)
  }
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cdt-shot-'))
  const browser = spawn(
    findBrowser(),
    [
      '--headless=new',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-networking',
      '--hide-scrollbars',
      '--force-color-profile=srgb',
      '--window-size=1440,980',
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
    await cdp.viewport(VIEWPORT)

    // 1. 登录页（此时还没登录）
    await cdp.goto(BASE)
    await sleep(800)
    await cdp.shot('01-login.png')

    // 2. 用接口登录，再重新进入应用
    const status = await cdp.evaluate(`fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password: 'test-password-12345' }),
    }).then((response) => response.status)`)
    if (status !== 200) throw new Error('登录失败，HTTP ' + status)

    await cdp.goto(BASE)
    await cdp.waitSelector('.app-shell')
    await sleep(1200)
    await cdp.shot('02-dashboard.png')

    // 2.0 「最近事件」默认只列 8 条，底下的「显示更多事件」负责展开剩下的（后端最多给 20 条）
    const recent = await cdp.evaluate(`(() => {
      const head = [...document.querySelectorAll('.section-head')].find((node) =>
        (node.textContent || '').includes('最近事件'))
      const more = document.querySelector('.events-more')
      return {
        rows: document.querySelectorAll('.events .event').length,
        badge: head ? (head.querySelector('.badge') || {}).textContent || '' : '',
        more: more ? more.textContent.trim() : '',
      }
    })()`)
    const eventRows = recent.rows
    const eventTotal = Number(recent.badge.trim() || 0)
    if (eventRows > 8) throw new Error(`最近事件默认显示了 ${eventRows} 条，应不超过 8 条`)
    if (eventTotal < eventRows) throw new Error(`事件角标 ${eventTotal} 小于列表 ${eventRows} 条`)
    if (eventTotal > 20) throw new Error(`概览一次给了 ${eventTotal} 条事件，后端上限应为 20`)
    if (eventTotal > eventRows) {
      if (!recent.more.includes('显示更多事件')) {
        throw new Error(`事件还有 ${eventTotal - eventRows} 条没露出来，却没找到「显示更多事件」按钮`)
      }
      await cdp.clickSelector('.events-more')
      await sleep(300)
      const expanded = await cdp.evaluate(`(() => ({
        rows: document.querySelectorAll('.events .event').length,
        more: (document.querySelector('.events-more') || {}).textContent || '',
      }))()`)
      if (expanded.rows <= eventRows) throw new Error('点了「显示更多事件」列表没变长')
      if (expanded.rows > eventTotal) throw new Error('展开后条数超过了总数')
      if (expanded.rows < eventTotal && !expanded.more.includes('显示更多事件')) {
        throw new Error('还没展开完，「显示更多事件」按钮就不见了')
      }
      console.log(
        `  ✓ 最近事件默认 8 条，展开后 ${eventRows} → ${expanded.rows}（共 ${eventTotal} 条）`,
      )
    } else {
      if (recent.more) throw new Error('事件已经全部露出来了，不该还有「显示更多事件」按钮')
      console.log(`  ✓ 最近事件 ${eventRows} 条全部列出，没有多余按钮`)
    }

    // 2.1 概览页前三个指标卡要能点进对应页面（顺序：服务器 / 单机保活 / 联合换班）
    const navByStat = await cdp.evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
      const heading = () => (document.querySelector('.main-area h1') || {}).textContent || ''
      const stats = () => Array.from(document.querySelectorAll('.stat-grid .stat-link'))
      const result = []
      for (const index of [0, 1, 2]) {
        const button = stats()[index]
        if (!button) return 'missing-stat-' + index
        button.click()
        await sleep(450)
        result.push(heading().trim())
        // 回到概览用的选择器写成按文字找，避免依赖 nav-item 的排列顺序
        const home = Array.from(document.querySelectorAll('.sidebar .nav-parent, .sidebar .nav-item'))
          .find((node) => (node.textContent || '').includes('概览'))
        if (!home) return 'missing-home'
        home.click()
        await sleep(350)
      }
      return result.join('|')
    })()`)
    if (navByStat !== '服务器|单机保活|联合换班') {
      throw new Error('概览指标卡跳转不对：' + navByStat)
    }
    console.log('  ✓ 概览三张指标卡可跳转（服务器/单机保活/联合换班）')
    await sleep(400)

    // 3. 左侧两个保活流程 + 服务器
    await cdp.clickByText('.sidebar .nav-item', '单机保活')
    await cdp.shot('03-keepalive.png')

    await cdp.clickByText('.sidebar .nav-item', '联合换班')
    await cdp.shot('04-shift.png')

    await cdp.clickByText('.sidebar .nav-item', '服务器')
    await cdp.shot('05-servers.png')
    const ownerCards = await cdp.evaluate(
      `[...document.querySelectorAll('.server-grid .owner-tag')].map((node) => node.textContent.trim())`,
    )

    // 3.1 列表视图：域名列的逻辑跟卡片一致（组域名只在当班那台），且不许被撑出横向滚动条
    await cdp.clickByText('.view-toggle button', '列表')
    await cdp.waitSelector('.server-table')
    await sleep(400)
    const tableCheck = await cdp.evaluate(`(() => {
      const card = document.querySelector('.server-table-card')
      const rows = [...document.querySelectorAll('.server-table tbody tr')]
      const dnsOf = (row) => {
        const cells = [...row.querySelectorAll('td')]
        return cells[7] ? cells[7].textContent.replace(/\\s+/g, '') : ''
      }
      return {
        overflow: card.scrollWidth - card.clientWidth,
        rows: rows.length,
        chips: document.querySelectorAll('.server-table tbody .dns-chip').length,
        dns: rows.map(dnsOf).join(' / '),
      }
    })()`)
    if (tableCheck.overflow > 1) {
      throw new Error('列表视图被撑出横向滚动：+' + tableCheck.overflow + 'px')
    }
    if (!tableCheck.rows || !tableCheck.chips) {
      throw new Error('列表视图没显示域名框：' + JSON.stringify(tableCheck))
    }
    console.log(`  ✓ 列表视图无横向滚动（${tableCheck.rows} 行 / ${tableCheck.chips} 个域名框）`)

    // 3.2 归属列的文字必须跟卡片一模一样（以前两处各写一套，措辞会飘）
    const ownerRows = await cdp.evaluate(
      `[...document.querySelectorAll('.server-table tbody .owner-tag')].map((node) => node.textContent.trim())`,
    )
    if (ownerCards.length !== ownerRows.length || ownerCards.some((text, index) => text !== ownerRows[index])) {
      throw new Error(
        '列表归属列和卡片不一致：卡片 ' + JSON.stringify(ownerCards) + ' / 列表 ' + JSON.stringify(ownerRows),
      )
    }
    console.log(`  ✓ 列表归属列与卡片一致（${ownerRows.length} 台）`)
    await cdp.shot('05b-servers-list.png')

    // 3.5 DNS 页：一条域名只绑一个目标，没绑的不动
    await cdp.clickByText('.sidebar .nav-parent', 'DNS')
    await cdp.waitSelector('.dns-row')
    await sleep(400)
    await cdp.shot('15-dns.png')
    const dnsRows = await cdp.evaluate(`(() => {
      const rows = [...document.querySelectorAll('.dns-row')]
      return rows.map((row) => {
        const name = row.querySelector('.dns-name')
        const badge = row.querySelector('.badge')
        const select = row.querySelector('select')
        const bound = select && select.selectedIndex >= 0 ? select.options[select.selectedIndex].text : ''
        return [name ? name.textContent.trim() : '', badge ? badge.textContent.trim() : '', bound.trim()].join('=')
      }).join('|')
    })()`)
    const expectedDns = [
      'cdt-demo.example.com=已同步=港新联合保活',
      'cdt-demo-2.example.com=已同步=日本-临时',
      'cdt-demo-3.example.com=目标未运行=德国-闲置',
    ].join('|')
    if (dnsRows !== expectedDns) {
      throw new Error('DNS 列表不对：' + dnsRows)
    }
    console.log('  ✓ DNS 三条记录各自绑到换班组 / 保活 / 闲散实例')

    // 4. 设置抽屉四个页签
    await cdp.clickByText('.sidebar-foot .nav-item', '设置')
    await cdp.waitSelector('.drawer')
    await cdp.shot('06-settings.png')

    await cdp.clickByText('.tabs button', 'DDNS')
    await cdp.shot('07-ddns.png')

    await cdp.clickByText('.tabs button', '通知')
    await cdp.shot('08-notify.png')

    // 关于页签顺手存一张，README 里讲"致敬上游"用得上
    await cdp.clickByText('.tabs button', '关于')
    await cdp.shot('09-about.png')

    await cdp.clickSelector('.drawer .close')

    // 5. 换班组成员编辑器
    await cdp.clickByText('.sidebar .nav-item', '联合换班')
    await cdp.clickSelector('.group-actions button[title="编辑"]')
    await cdp.waitSelector('.modal')
    await cdp.shot('10-group-dialog.png')
    await cdp.clickSelector('.modal .close')

    // 5.1 新建组：成员框留空由用户自己选；选择器里已编组的机器要是灰的
    await cdp.clickByText('.page-actions button', '新建换班组')
    await cdp.waitSelector('.member-pick')
    const emptyPicks = await cdp.evaluate(
      `[...document.querySelectorAll('.member-pick')].map((node) => node.textContent.trim())`,
    )
    if (!emptyPicks.length || emptyPicks.some((text) => text !== '选择服务器')) {
      throw new Error('新建组默认没留空：' + JSON.stringify(emptyPicks))
    }
    await cdp.clickSelector('.member-pick')
    await cdp.waitSelector('.server-pick-list')
    await sleep(400)
    const pickRows = await cdp.evaluate(`[...document.querySelectorAll('.server-pick-row')].map((node) => ({
      owner: node.querySelector('.owner-tag').textContent.trim(),
      disabled: node.disabled,
    }))`)
    if (!pickRows.some((row) => row.owner.includes('换班组') && row.disabled)) {
      throw new Error('选择器里已加入换班组的机器不是灰的：' + JSON.stringify(pickRows))
    }
    if (!pickRows.some((row) => row.owner.includes('闲散') && !row.disabled)) {
      throw new Error('选择器里没有可选的闲散机器')
    }
    await cdp.shot('10b-group-picker.png')
    await cdp.evaluate(`[...document.querySelectorAll('.modal')].pop().querySelector('.close').click()`)
    await sleep(400)
    await cdp.clickSelector('.modal .close')
    await sleep(300)
    console.log('  ✓ 新建组默认留空，选择器按归属灰选')

    // 6. 添加服务器：站点类型 + 按站点过滤的地域
    await cdp.clickByText('.sidebar .nav-item', '服务器')
    await cdp.clickByText('.page-actions button', '添加服务器')
    await cdp.waitSelector('.modal')
    await sleep(400)
    await cdp.shot('11-server-dialog.png')

    // 回归检查：以前 App 每秒重渲染会把焦点从 <select> 上抢走，
    // 表现为地域下拉框点开就关。现在按住 1.6 秒焦点还在，说明真的修好了。
    const focusKept = await cdp.evaluate(`(async () => {
      const select = document.querySelector('#region')
      if (!select) return 'no-select'
      select.focus()
      const before = document.activeElement && document.activeElement.id
      await new Promise((resolve) => setTimeout(resolve, 1600))
      const after = document.activeElement && document.activeElement.id
      return before + '->' + after
    })()`)
    if (focusKept !== 'region->region') {
      throw new Error('地域选择框焦点被抢走了：' + focusKept)
    }
    console.log('  ✓ 地域下拉框焦点稳定（1.6s 未被抢）')

    // 国际站的地域列表不该出现中国大陆
    const intlOk = await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('.segmented button')]
      const button = buttons.find((item) => item.textContent.includes('国际站'))
      if (!button) return 'no-button'
      button.click()
      return 'clicked'
    })()`)
    await sleep(300)
    const intlRegions = await cdp.evaluate(`(() => {
      const select = document.querySelector('#region')
      if (!select) return 'no-select'
      const values = [...select.querySelectorAll('option')].map((item) => item.value)
      return values.filter((value) => value.startsWith('cn-') && value !== 'cn-hongkong').join(',')
    })()`)
    if (intlOk !== 'clicked' || intlRegions !== '') {
      throw new Error('国际站仍出现中国大陆地域：' + intlRegions)
    }
    console.log('  ✓ 国际站已过滤掉中国大陆地域')
    await cdp.shot('12-server-dialog-intl.png')
    await cdp.clickSelector('.modal .close')

    // 7. 删除要二次确认。以前用 window.confirm，桌面壳里会被静默吞掉 → 点一下直接删了。
    //    这里断言弹窗真的出现、标题对得上，再点「取消」保住演示数据。
    await cdp.clickByText('.sidebar .nav-item', '服务器')
    await cdp.clickSelector('.server-card .actions button[title="删除"]')
    await cdp.waitSelector('.modal')
    await sleep(300)
    await cdp.shot('14-confirm-delete.png')
    const confirmBox = await cdp.evaluate(`(() => {
      const box = document.querySelector('.modal')
      if (!box) return 'no-modal'
      const pick = (selector) => {
        const node = box.querySelector(selector)
        return node ? node.textContent.trim() : ''
      }
      return [pick('.modal-head h3'), pick('.confirm-text'), pick('.confirm-hint')].join('|')
    })()`)
    if (!confirmBox.startsWith('删除服务器|确定删除')) {
      throw new Error('删除确认弹窗不对劲：' + confirmBox)
    }
    console.log('  ✓ 删除服务器要二次确认（应用内弹窗）')
    await cdp.clickByText('.modal-foot button', '取消')
    if (await cdp.evaluate(`document.querySelector('.modal') ? 1 : 0`)) {
      throw new Error('点取消后弹窗没关')
    }
    console.log('  ✓ 取消后弹窗关闭，演示数据没被删')

    // 7. 窄屏：手机上任何一页都不许横向滚动（原来是整页比屏幕宽，得左右拖）
    await cdp.viewport(MOBILE, true)
    await cdp.goto(BASE)
    await cdp.waitSelector('.app-shell')
    await sleep(1000)
    await cdp.shot('13-mobile.png')

    const wide = []
    for (const [label, nav] of [
      ['概览', null],
      ['单机保活', '单机保活'],
      ['联合换班', '联合换班'],
      ['服务器', '服务器'],
      ['服务器列表', '服务器'],
      ['DNS', 'DNS'],
    ]) {
      if (nav === 'DNS') await cdp.clickByText('.sidebar .nav-parent', 'DNS')
      else if (nav) await cdp.clickByText('.sidebar .nav-item', nav)
      if (label === '服务器列表') await cdp.clickByText('.view-toggle button', '列表')
      await sleep(800)
      const overflow = await cdp.evaluate(
        `(() => {
          const vw = document.documentElement.clientWidth
          const doc = document.documentElement.scrollWidth
          const card = document.querySelector('.server-table-card')
          return { vw, doc, table: card ? card.scrollWidth - card.clientWidth : 0 }
        })()`,
      )
      if (overflow.doc > overflow.vw + 1 || overflow.table > 1) {
        wide.push(`${label}（页面 ${overflow.doc}/${overflow.vw}，表格 +${overflow.table}）`)
      }
      if (label === '服务器列表') {
        await cdp.clickByText('.view-toggle button', '卡片')
        await sleep(400)
      }
    }
    if (wide.length) {
      throw new Error('窄屏还需要横向滚动：' + wide.join('；'))
    }
    console.log('  ✓ 窄屏（' + MOBILE.width + 'px）六个视图都不需要横向滚动')

    console.log('\n截图已输出到 ' + OUT)
  } finally {
    if (cdp) {
      try {
        await cdp.send('Browser.close')
      } catch (_error) {
        /* ignore */
      }
    }
    browser.kill()
    await sleep(400)
    try {
      fs.rmSync(profile, { recursive: true, force: true })
    } catch (_error) {
      /* Windows 上偶发占用，忽略 */
    }
  }
})().catch((error) => {
  console.error('截图失败：' + error.message)
  process.exit(1)
})
