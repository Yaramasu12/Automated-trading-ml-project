import { clsx } from 'clsx'
import {
  Activity,
  BarChart2,
  BookOpen,
  Brain,
  ChevronRight,
  CircleDot,
  Cpu,
  GitBranch,
  LayoutDashboard,
  Newspaper,
  Power,
  Shield,
  Wifi,
  WifiOff,
  Zap,
  Bot,
  Menu,
  X,
  MoreHorizontal,
} from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { useStore } from '../store'
import type { NavView } from '../store'
import { execModeBadge } from './shared/Badge'
import { useDashboardWs } from '../ws'

interface NavItem {
  id: NavView
  label: string
  icon: ReactNode
  shortLabel?: string
}

const NAV: NavItem[] = [
  { id: 'dashboard',    label: 'Dashboard',   shortLabel: 'Home',      icon: <LayoutDashboard size={16} /> },
  { id: 'engine',       label: 'Engine',      shortLabel: 'Agent',     icon: <Bot size={16} /> },
  { id: 'signals',      label: 'Signals',     shortLabel: 'Signals',   icon: <Zap size={16} /> },
  { id: 'strategies',   label: 'Strategies',  shortLabel: 'Strategy',  icon: <BarChart2 size={16} /> },
  { id: 'backtest',     label: 'Backtest',    shortLabel: 'Backtest',  icon: <BookOpen size={16} /> },
  { id: 'models',       label: 'Models',      shortLabel: 'Models',    icon: <Brain size={16} /> },
  { id: 'risk',         label: 'Risk',        shortLabel: 'Risk',      icon: <Shield size={16} /> },
  { id: 'execution',    label: 'Execution',   shortLabel: 'Orders',    icon: <GitBranch size={16} /> },
  { id: 'intelligence', label: 'Intel',       shortLabel: 'Intel',     icon: <Newspaper size={16} /> },
  { id: 'account',      label: 'Account',     shortLabel: 'Account',   icon: <Cpu size={16} /> },
]

// Bottom tab bar shows 4 primary tabs + a "More" button on mobile
const BOTTOM_TABS: NavItem[] = NAV.slice(0, 4)

export function Layout({ children }: { children: ReactNode }) {
  const { send } = useDashboardWs()
  const [drawerOpen, setDrawerOpen] = useState(false)

  const activeView    = useStore((s) => s.activeView)
  const setActiveView = useStore((s) => s.setActiveView)
  const wsConnected   = useStore((s) => s.wsConnected)
  const runtimeState  = useStore((s) => s.runtimeState)
  const monitoring    = useStore((s) => s.monitoring)

  const mode      = runtimeState?.execution_mode ?? '...'
  const killActive = runtimeState?.kill_switch_active ?? false

  function toggleKillSwitch() {
    send({ action: 'kill_switch', active: !killActive })
  }

  function navigate(id: NavView) {
    setActiveView(id)
    setDrawerOpen(false)
  }

  return (
    <div className="flex h-screen bg-surface overflow-hidden">

      {/* ── DESKTOP SIDEBAR ── (hidden on mobile) ─────────────────────────── */}
      <aside className="hidden md:flex w-56 flex-shrink-0 flex-col border-r border-surface-border bg-surface-card">
        {/* Logo */}
        <div className="px-4 py-4 border-b border-surface-border">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-md bg-brand-blue/20 flex items-center justify-center">
              <Activity size={14} className="text-brand-blue" />
            </div>
            <div>
              <div className="text-xs font-bold text-gray-100 leading-none">AI Trading</div>
              <div className="text-[10px] text-gray-500 mt-0.5">Angel One · NSE/BSE</div>
            </div>
          </div>
        </div>

        {/* Nav items */}
        <nav className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
          {NAV.map((item) => (
            <button
              key={item.id}
              onClick={() => navigate(item.id)}
              className={clsx(
                'w-full flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors',
                activeView === item.id
                  ? 'bg-brand-blue/15 text-brand-blue'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-surface-elevated',
              )}
            >
              {item.icon}
              <span className="font-medium">{item.label}</span>
              {activeView === item.id && <ChevronRight size={12} className="ml-auto" />}
            </button>
          ))}
        </nav>

        {/* Status footer */}
        <div className="px-3 py-3 border-t border-surface-border space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-gray-500 uppercase tracking-wider">Connection</span>
            <div className="flex items-center gap-1">
              {wsConnected
                ? <Wifi size={11} className="text-brand-green" />
                : <WifiOff size={11} className="text-brand-red" />}
              <span className={clsx('text-[10px] font-mono', wsConnected ? 'text-brand-green' : 'text-brand-red')}>
                {wsConnected ? 'LIVE' : 'OFF'}
              </span>
            </div>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-gray-500 uppercase tracking-wider">Mode</span>
            <div className="scale-90 origin-right">{execModeBadge(mode)}</div>
          </div>
          {monitoring && (
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-gray-500 uppercase tracking-wider">Orders</span>
              <span className="text-[10px] font-mono text-gray-300">{monitoring.total_orders}</span>
            </div>
          )}
        </div>
      </aside>

      {/* ── MOBILE DRAWER OVERLAY ─────────────────────────────────────────── */}
      {drawerOpen && (
        <div
          className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm"
          onClick={() => setDrawerOpen(false)}
        />
      )}
      <aside
        className={clsx(
          'md:hidden fixed top-0 left-0 h-full w-64 z-50 flex flex-col',
          'bg-surface-card border-r border-surface-border shadow-2xl',
          'transition-transform duration-300 ease-in-out',
          drawerOpen ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        {/* Drawer header */}
        <div className="px-4 py-4 border-b border-surface-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-md bg-brand-blue/20 flex items-center justify-center">
              <Activity size={14} className="text-brand-blue" />
            </div>
            <div>
              <div className="text-xs font-bold text-gray-100 leading-none">AI Trading</div>
              <div className="text-[10px] text-gray-500 mt-0.5">Angel One · NSE/BSE</div>
            </div>
          </div>
          <button
            onClick={() => setDrawerOpen(false)}
            className="p-1.5 rounded-md text-gray-400 hover:text-gray-200 hover:bg-surface-elevated"
          >
            <X size={16} />
          </button>
        </div>

        {/* Drawer nav */}
        <nav className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
          {NAV.map((item) => (
            <button
              key={item.id}
              onClick={() => navigate(item.id)}
              className={clsx(
                'w-full flex items-center gap-3 px-3 py-3 rounded-md text-sm transition-colors',
                activeView === item.id
                  ? 'bg-brand-blue/15 text-brand-blue'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-surface-elevated',
              )}
            >
              {item.icon}
              <span className="font-medium">{item.label}</span>
              {activeView === item.id && <ChevronRight size={12} className="ml-auto" />}
            </button>
          ))}
        </nav>

        {/* Drawer status */}
        <div className="px-4 py-4 border-t border-surface-border space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">Connection</span>
            <div className="flex items-center gap-1.5">
              {wsConnected
                ? <Wifi size={12} className="text-brand-green" />
                : <WifiOff size={12} className="text-brand-red" />}
              <span className={clsx('text-xs font-mono', wsConnected ? 'text-brand-green' : 'text-brand-red')}>
                {wsConnected ? 'LIVE' : 'OFFLINE'}
              </span>
            </div>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">Mode</span>
            {execModeBadge(mode)}
          </div>
          {/* Kill switch inside drawer too */}
          <button
            onClick={() => { toggleKillSwitch(); setDrawerOpen(false) }}
            className={clsx(
              'w-full flex items-center justify-center gap-2 py-2.5 rounded-md text-sm font-semibold border transition-all',
              killActive
                ? 'bg-brand-red/20 border-brand-red/50 text-brand-red'
                : 'bg-surface-elevated border-surface-border text-gray-400',
            )}
          >
            <Power size={14} />
            {killActive ? 'KILL SWITCH ACTIVE' : 'Kill Switch OFF'}
          </button>
        </div>
      </aside>

      {/* ── MAIN CONTENT AREA ─────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0">

        {/* Top bar */}
        <header className="h-12 flex-shrink-0 flex items-center justify-between px-4 border-b border-surface-border bg-surface-card">
          <div className="flex items-center gap-3">
            {/* Hamburger — mobile only */}
            <button
              className="md:hidden p-1 rounded-md text-gray-400 hover:text-gray-200 active:bg-surface-elevated"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open menu"
            >
              <Menu size={18} />
            </button>

            <CircleDot
              size={14}
              className={clsx(
                monitoring?.status === 'HEALTHY' ? 'text-brand-green' : 'text-brand-red',
                'animate-pulse-slow',
              )}
            />
            <span className="text-xs text-gray-400 font-mono hidden sm:block">
              {monitoring?.status ?? 'CONNECTING'}
            </span>
            <span className="text-xs text-gray-600 font-mono hidden md:block">
              latency {monitoring?.average_latency_ms.toFixed(1) ?? '—'}ms
            </span>
          </div>

          <div className="flex items-center gap-2">
            {/* WS indicator — mobile */}
            <div className="md:hidden flex items-center gap-1">
              {wsConnected
                ? <Wifi size={12} className="text-brand-green" />
                : <WifiOff size={12} className="text-brand-red" />}
            </div>

            {/* Kill switch */}
            <button
              onClick={toggleKillSwitch}
              className={clsx(
                'flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium border transition-all',
                killActive
                  ? 'bg-brand-red/20 border-brand-red/50 text-brand-red'
                  : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
              )}
            >
              <Power size={12} />
              <span className="hidden sm:inline">{killActive ? 'KILL ACTIVE' : 'Kill Switch'}</span>
            </button>
          </div>
        </header>

        {/* Page content — extra bottom padding on mobile for tab bar */}
        <main className="flex-1 overflow-y-auto p-4 md:p-5 pb-20 md:pb-5 animate-fade-in">
          {children}
        </main>
      </div>

      {/* ── MOBILE BOTTOM TAB BAR ─────────────────────────────────────────── */}
      <nav className="md:hidden fixed bottom-0 left-0 right-0 z-30 bg-surface-card border-t border-surface-border">
        <div className="flex items-center justify-around px-1 py-1 safe-area-bottom">
          {BOTTOM_TABS.map((item) => (
            <button
              key={item.id}
              onClick={() => navigate(item.id)}
              className={clsx(
                'flex flex-col items-center gap-0.5 px-3 py-2 rounded-lg transition-colors min-w-0 flex-1',
                activeView === item.id
                  ? 'text-brand-blue bg-brand-blue/10'
                  : 'text-gray-500 active:bg-surface-elevated',
              )}
            >
              {item.icon}
              <span className="text-[9px] font-medium truncate">{item.shortLabel ?? item.label}</span>
            </button>
          ))}

          {/* "More" opens the drawer */}
          <button
            onClick={() => setDrawerOpen(true)}
            className={clsx(
              'flex flex-col items-center gap-0.5 px-3 py-2 rounded-lg transition-colors min-w-0 flex-1',
              !BOTTOM_TABS.find(t => t.id === activeView)
                ? 'text-brand-blue bg-brand-blue/10'
                : 'text-gray-500 active:bg-surface-elevated',
            )}
          >
            <MoreHorizontal size={16} />
            <span className="text-[9px] font-medium">More</span>
          </button>
        </div>
      </nav>

    </div>
  )
}
