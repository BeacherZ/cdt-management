import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Gauge,
  HeartPulse,
  LogOut,
  Plus,
  RefreshCw,
  Repeat,
  Server as ServerIcon,
  Settings as SettingsIcon,
  Zap,
} from 'lucide-react'
import { APIError, api, del, post } from './api'
import type { InitStatus, Keepalive, Overview, Server, Settings, ShiftGroup } from './types'
import Auth from './components/Auth'
import DashboardView from './components/DashboardView'
import KeepaliveView from './components/KeepaliveView'
import ServersView from './components/ServersView'
import ShiftView from './components/ShiftView'
import ServerDialog from './components/ServerDialog'
import GroupDialog from './components/GroupDialog'
import SettingsDrawer from './components/SettingsDrawer'
import { Badge, Empty, Modal, statusLabel, statusTone } from './components/ui'

type Phase = 'loading' | 'setup' | 'login' | 'ready' | 'fatal'
type View = 'dashboard' | 'keepalive' | 'shift' | 'servers'
type Toast = { id: number; tone: 'success' | 'error' | 'info'; message: string }

const REFRESH_MS = 10_000

type NavLeaf = { key: View; label: string; icon: typeof Gauge }

/**
 * 侧栏只有两个总菜单：概览 / 保活。
 * 带 key 的总菜单本身就是页面（概览可点），不带的只作分组标签（保活）。
 * 子项统一缩进挂在总菜单下面。
 */
type NavSection = {
  title: string
  key?: View
  icon?: typeof Gauge
  children: NavLeaf[]
}

const NAV_SECTIONS: NavSection[] = [
  {
    title: '概览',
    key: 'dashboard',
    icon: Gauge,
    children: [{ key: 'servers', label: '服务器', icon: ServerIcon }],
  },
  {
    title: '保活',
    icon: Activity,
    children: [
      { key: 'keepalive', label: '单机保活', icon: HeartPulse },
      { key: 'shift', label: '联合换班', icon: Repeat },
    ],
  },
]

function navCount(key: View, overview: Overview): number {
  if (key === 'servers') return overview.servers.length
  if (key === 'keepalive') return overview.keepalives.length
  if (key === 'shift') return overview.groups.length
  return 0
}

export default function App() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [view, setView] = useState<View>('dashboard')
  const [meta, setMeta] = useState<InitStatus>({
    initialized: false,
    brand: 'CDT-Management',
    version: '',
    repo: '',
  })
  const [overview, setOverview] = useState<Overview | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [fatal, setFatal] = useState('')
  const [toasts, setToasts] = useState<Toast[]>([])
  const [busyServer, setBusyServer] = useState<number | null>(null)
  const [busyGroup, setBusyGroup] = useState<number | null>(null)
  const [serverDialog, setServerDialog] = useState<{ open: boolean; server: Server | null }>({
    open: false,
    server: null,
  })
  const [groupDialog, setGroupDialog] = useState<{ open: boolean; group: ShiftGroup | null }>({
    open: false,
    group: null,
  })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [keepalivePicker, setKeepalivePicker] = useState(false)
  const polling = useRef<number | null>(null)

  const toast = useCallback((message: string, tone: Toast['tone'] = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((current) => [...current, { id, message, tone }])
    window.setTimeout(() => setToasts((current) => current.filter((item) => item.id !== id)), 4200)
  }, [])

  const loadAll = useCallback(async () => {
    const [nextOverview, nextSettings] = await Promise.all([
      api<Overview>('/api/v1/overview'),
      api<Settings>('/api/v1/settings'),
    ])
    setOverview(nextOverview)
    setSettings(nextSettings)
    setPhase('ready')
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        const init = await api<InitStatus>('/api/v1/system/init-status')
        setMeta(init)
        if (!init.initialized) {
          setPhase('setup')
          return
        }
        await loadAll()
      } catch (error) {
        if (error instanceof APIError && error.status === 401) {
          setPhase('login')
          return
        }
        setFatal(error instanceof Error ? error.message : '无法连接后端')
        setPhase('fatal')
      }
    })()
  }, [loadAll])

  useEffect(() => {
    if (phase !== 'ready') {
      if (polling.current) window.clearInterval(polling.current)
      return
    }
    polling.current = window.setInterval(() => {
      void api<Overview>('/api/v1/overview')
        .then(setOverview)
        .catch((error) => {
          if (error instanceof APIError && error.status === 401) setPhase('login')
        })
    }, REFRESH_MS)
    return () => {
      if (polling.current) window.clearInterval(polling.current)
    }
  }, [phase])

  async function reconcile() {
    try {
      await post('/api/v1/reconcile')
      await loadAll()
      toast('已触发一轮对账', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '对账失败', 'error')
    }
  }

  async function serverAction(serverId: number, action: 'start' | 'stop') {
    setBusyServer(serverId)
    try {
      const result = await post<{ managed_note?: string }>(
        `/api/v1/servers/${serverId}/actions/${action}`,
      )
      await loadAll()
      toast(action === 'start' ? '已下发开机指令' : '已下发节省停机指令', 'success')
      if (result?.managed_note) {
        toast(`${result.managed_note}托管中，下一轮对账会按流程纠正`, 'info')
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : '操作失败', 'error')
    } finally {
      setBusyServer(null)
    }
  }

  async function refreshServer(serverId: number) {
    setBusyServer(serverId)
    try {
      await post(`/api/v1/servers/${serverId}/refresh`)
      await loadAll()
      toast('已刷新', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '刷新失败', 'error')
    } finally {
      setBusyServer(null)
    }
  }

  async function deleteServer(server: Server) {
    if (!window.confirm(`删除服务器「${server.label}」？配置会一并删除，实例本身不受影响。`)) return
    try {
      await del(`/api/v1/servers/${server.id}`)
      await loadAll()
      toast('服务器已删除', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '删除失败', 'error')
    }
  }

  async function switchGroup(group: ShiftGroup) {
    setBusyGroup(group.id)
    try {
      await post(`/api/v1/groups/${group.id}/switch`)
      await loadAll()
      toast('已切换到下一台', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '换班失败', 'error')
    } finally {
      setBusyGroup(null)
    }
  }

  async function deleteGroup(group: ShiftGroup) {
    if (!window.confirm(`删除换班组「${group.name}」？服务器不会被删除。`)) return
    try {
      await del(`/api/v1/groups/${group.id}`)
      await loadAll()
      toast('换班组已删除', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '删除失败', 'error')
    }
  }

  async function addKeepalive(serverId: number) {
    try {
      await post('/api/v1/keepalives', { server_id: serverId })
      setKeepalivePicker(false)
      await loadAll()
      toast('已加入单机保活', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '添加失败', 'error')
    }
  }

  async function toggleKeepalive(item: Keepalive, enabled: boolean) {
    try {
      await post(`/api/v1/keepalives/${item.id}/toggle`, { enabled })
      await loadAll()
    } catch (error) {
      toast(error instanceof Error ? error.message : '操作失败', 'error')
    }
  }

  async function removeKeepalive(item: Keepalive) {
    if (!window.confirm(`把「${item.label}」移出单机保活？`)) return
    try {
      await del(`/api/v1/keepalives/${item.id}`)
      await loadAll()
      toast('已移出保活', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '操作失败', 'error')
    }
  }

  async function logout() {
    try {
      await post('/api/v1/auth/logout')
    } finally {
      setPhase('login')
      setOverview(null)
    }
  }

  if (phase === 'loading') {
    return (
      <div className="boot">
        <div className="spinner" />
        正在连接…
      </div>
    )
  }

  if (phase === 'fatal') {
    return (
      <div className="boot">
        <AlertTriangle size={22} style={{ color: 'var(--red)' }} />
        <div>{fatal}</div>
        <button className="btn" onClick={() => window.location.reload()}>
          重试
        </button>
      </div>
    )
  }

  if (phase === 'setup' || phase === 'login') {
    return (
      <Auth
        mode={phase}
        brand={meta.brand || 'CDT-Management'}
        onDone={() => {
          setPhase('loading')
          void loadAll().catch(() => setPhase('login'))
        }}
      />
    )
  }

  if (!overview || !settings) {
    return (
      <div className="boot">
        <div className="spinner" />
      </div>
    )
  }

  const keepaliveIds = new Set(overview.keepalives.map((item) => item.server_id))
  // 已编进换班组的机器按班次自动启停，单机保活会自动让位，界面上不再给"加入"入口
  const groupedIds = new Set(
    overview.groups.flatMap((group) => group.members.map((member) => member.server_id)),
  )
  const available = overview.servers.filter(
    (server) => !keepaliveIds.has(server.id) && !groupedIds.has(server.id),
  )

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <Repeat size={18} />
          </div>
          <div className="brand-text">
            <strong>{overview.brand}</strong>
          </div>
        </div>

        <nav className="nav-list" aria-label="主视图">
          {NAV_SECTIONS.map((section) => {
            const parentKey = section.key
            const ParentIcon = section.icon
            const sectionActive =
              parentKey === view || section.children.some((leaf) => leaf.key === view)
            return (
              <div
                className={`nav-group${sectionActive ? ' is-active' : ''}`}
                key={section.title}
              >
                {parentKey && ParentIcon ? (
                  <button
                    className={`nav-parent${view === parentKey ? ' on' : ''}`}
                    aria-current={view === parentKey ? 'page' : undefined}
                    onClick={() => setView(parentKey)}
                  >
                    <ParentIcon size={15} />
                    <span className="nav-text">{section.title}</span>
                  </button>
                ) : (
                  <div className="nav-parent label">{section.title}</div>
                )}

                <div className="nav-sub">
                  {section.children.map((item) => {
                    const Icon = item.icon
                    const count = navCount(item.key, overview)
                    return (
                      <button
                        key={item.key}
                        className={`nav-item${view === item.key ? ' on' : ''}`}
                        aria-current={view === item.key ? 'page' : undefined}
                        onClick={() => setView(item.key)}
                      >
                        <Icon size={14} />
                        <span className="nav-text">{item.label}</span>
                        {count > 0 && <span className="nav-count">{count}</span>}
                      </button>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </nav>

        <div className="sidebar-foot">
          <button className="nav-item" onClick={() => setSettingsOpen(true)}>
            <SettingsIcon size={15} />
            <span className="nav-text">设置</span>
          </button>
          <button className="nav-item" onClick={logout}>
            <LogOut size={15} />
            <span className="nav-text">退出登录</span>
          </button>
          <div className="meta" style={{ paddingLeft: 11, marginTop: 4 }}>
            <Zap size={10} /> v{overview.version}
          </div>
        </div>
      </aside>

      <main className="main-area">
        {view === 'dashboard' && <DashboardView overview={overview} />}

        {view === 'keepalive' && (
          <KeepaliveView
            keepalives={overview.keepalives}
            servers={overview.servers}
            groupedIds={groupedIds}
            busyId={busyServer}
            onAdd={() => setKeepalivePicker(true)}
            onToggle={toggleKeepalive}
            onRemove={removeKeepalive}
            onAction={serverAction}
            onRefresh={refreshServer}
          />
        )}

        {view === 'shift' && (
          <ShiftView
            groups={overview.groups}
            serverCount={overview.servers.length}
            busyId={busyGroup}
            onAdd={() => setGroupDialog({ open: true, group: null })}
            onEdit={(group) => setGroupDialog({ open: true, group })}
            onDelete={deleteGroup}
            onSwitch={switchGroup}
          />
        )}

        {view === 'servers' && (
          <ServersView
            servers={overview.servers}
            busyId={busyServer}
            onAdd={() => setServerDialog({ open: true, server: null })}
            onEdit={(server) => setServerDialog({ open: true, server })}
            onDelete={deleteServer}
            onRefresh={(server) => refreshServer(server.id)}
            onAction={(server, action) => serverAction(server.id, action)}
            onAddToKeepalive={async (server) => {
              if (keepaliveIds.has(server.id)) {
                toast('这台已经在单机保活里了', 'info')
                return
              }
              await addKeepalive(server.id)
            }}
          />
        )}

        <div
          style={{
            marginTop: 28,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            fontSize: 11.5,
            color: 'var(--muted-soft)',
          }}
        >
          <button className="btn ghost sm" onClick={reconcile}>
            <RefreshCw size={12} /> 立即对账
          </button>
          <span>
            对账间隔 {settings.reconcile_interval} 秒 · {overview.timezone}
          </span>
          {!settings.sdk_ready && <Badge tone="amber">阿里云 SDK 未就绪</Badge>}
          {settings.dry_run && <Badge tone="amber">DRY RUN</Badge>}
        </div>
      </main>

      {serverDialog.open && (
        <ServerDialog
          server={serverDialog.server}
          defaultThresholdPercent={Number(settings.traffic_threshold_pct) || 94}
          onClose={() => setServerDialog({ open: false, server: null })}
          onSaved={async (message) => {
            setServerDialog({ open: false, server: null })
            await loadAll()
            toast(message, 'success')
          }}
        />
      )}

      {groupDialog.open && (
        <GroupDialog
          group={groupDialog.group}
          servers={overview.servers}
          onClose={() => setGroupDialog({ open: false, group: null })}
          onSaved={async (message) => {
            setGroupDialog({ open: false, group: null })
            await loadAll()
            toast(message, 'success')
          }}
        />
      )}

      {keepalivePicker && (
        <Modal
          title="加入单机保活"
          icon={<HeartPulse size={16} style={{ color: 'var(--green)' }} />}
          onClose={() => setKeepalivePicker(false)}
        >
          {available.length === 0 ? (
            <Empty icon={<Plus size={18} />} title="没有可添加的服务器">
              所有服务器都已在单机保活或换班组里了。编进换班组的机器按班次自动启停，不会再进单机保活。
            </Empty>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {available.map((server) => (
                <button
                  key={server.id}
                  className="slot"
                  style={{ textAlign: 'left', cursor: 'pointer' }}
                  onClick={() => addKeepalive(server.id)}
                >
                  <div className="slot-top">
                    <Badge tone={statusTone(server.instance_status)}>
                      {statusLabel(server.instance_status)}
                    </Badge>
                  </div>
                  <div className="slot-name">{server.label}</div>
                  <div className="slot-line">
                    <code>{server.instance_id || '未填实例'}</code>
                    <span>{server.public_ip || '无公网 IP'}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </Modal>
      )}

      {settingsOpen && (
        <SettingsDrawer
          settings={settings}
          onClose={() => setSettingsOpen(false)}
          onSaved={async (message) => {
            setSettingsOpen(false)
            await loadAll()
            toast(message, 'success')
          }}
        />
      )}

      <div className="toasts">
        {toasts.map((item) => (
          <div className={`toast ${item.tone}`} key={item.id}>
            {item.message}
          </div>
        ))}
      </div>
    </div>
  )
}
