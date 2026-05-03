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
} from 'lucide-react'
import type { ReactNode } from 'react'
import { useStore } from '../store'
import type { NavView } from '../store'
import { execModeBadge } from './shared/Badge'
import { useDashboardWs } from '../ws'

interface NavItem {
  id: NavView
  label: string
  icon: ReactNode
}

const NAV: NavItem[] = [
  { id: 'dashboard', label: 'Dashboard', icon: <LayoutDashboard size={16} /> },
  { id: 'signals', label: 'Signals', icon: <Zap size={16} /> },
  { id: 'strategies', label: 'Strategies', icon: <BarChart2 size={16} /> },
  { id: 'backtest', label: 'Backtest', icon: <BookOpen size={16} /> },
  { id: 'models', label: 'Models', icon: <Brain size={16} /> },
  { id: 'risk', label: 'Risk', icon: <Shield size={16} /> },
  { id: 'execution', label: 'Execution', icon: <GitBranch size={16} /> },
  { id: 'intelligence', label: 'Intel', icon: <Newspaper size={16} /> },
  { id: 'account', label: 'Account', icon: <Cpu size={16} /> },
]

export function Layout({ children }: { children: ReactNode }) {
  const { send } = useDashboardWs()

  const activeView = useStore((s) => s.activeView)
  const setActiveView = useStore((s) => s.setActiveView)
  const wsConnected = useStore((s) => s.wsConnected)
  const runtimeState = useStore((s) => s.runtimeState)
  const monitoring = useStore((s) => s.monitoring)

  const mode = runtimeState?.execution_mode ?? '...'
  const killActive = runtimeState?.kill_switch_active ?? false

  function toggleKillSwitch() {
    send({ action: 'kill_switch', active: !killActive })
  }

  return (
    <div className="flex h-screen bg-surface overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 flex flex-col border-r border-surface-border bg-surface-card">
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
              onClick={() => setActiveView(item.id)}
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
              {wsConnected ? (
                <Wifi size={11} className="text-brand-green" />
              ) : (
                <WifiOff size={11} className="text-brand-red" />
              )}
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

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <header className="h-12 flex-shrink-0 flex items-center justify-between px-5 border-b border-surface-border bg-surface-card">
          <div className="flex items-center gap-3">
            <CircleDot
              size={14}
              className={clsx(
                monitoring?.status === 'HEALTHY' ? 'text-brand-green' : 'text-brand-red',
                'animate-pulse-slow',
              )}
            />
            <span className="text-xs text-gray-400 font-mono">
              {monitoring?.status ?? 'CONNECTING'}
            </span>
            {monitoring && (
              <span className="text-xs text-gray-600 font-mono hidden md:block">
                latency {monitoring.average_latency_ms.toFixed(1)}ms
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            {/* Kill switch */}
            <button
              onClick={toggleKillSwitch}
              className={clsx(
                'flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-medium border transition-all',
                killActive
                  ? 'bg-brand-red/20 border-brand-red/50 text-brand-red hover:bg-brand-red/30'
                  : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
              )}
            >
              <Power size={12} />
              {killActive ? 'KILL ACTIVE' : 'Kill Switch'}
            </button>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-5 animate-fade-in">{children}</main>
      </div>
    </div>
  )
}
