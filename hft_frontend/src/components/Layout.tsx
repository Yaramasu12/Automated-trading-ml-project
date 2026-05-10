import { clsx } from 'clsx'
import {
  Activity,
  Atom,
  BarChart2,
  BookOpen,
  Bot,
  Brain,
  ChevronRight,
  Cpu,
  Database,
  FlaskConical,
  GitBranch,
  LayoutDashboard,
  Menu,
  MoreHorizontal,
  Newspaper,
  Power,
  Shield,
  ShieldCheck,
  Swords,
  Target,
  Waypoints,
  Wifi,
  WifiOff,
  X,
  Zap,
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
  accent?: string
}

interface NavSection {
  title: string
  items: NavItem[]
}

const NAV_SECTIONS: NavSection[] = [
  {
    title: 'Trading',
    items: [
      { id: 'dashboard',    label: 'Dashboard',    shortLabel: 'Home',     icon: <LayoutDashboard size={15} /> },
      { id: 'engine',       label: 'Engine',       shortLabel: 'Engine',   icon: <Bot size={15} /> },
      { id: 'signals',      label: 'Signals',      shortLabel: 'Signals',  icon: <Zap size={15} /> },
      { id: 'strategies',   label: 'Strategies',   shortLabel: 'Strategy', icon: <BarChart2 size={15} /> },
    ],
  },
  {
    title: 'Analysis',
    items: [
      { id: 'backtest',     label: 'Backtest',     shortLabel: 'Backtest', icon: <BookOpen size={15} /> },
      { id: 'tournament',   label: 'Tournament',   shortLabel: 'Tourney',  icon: <Swords size={15} />, accent: 'orange' },
      { id: 'models',       label: 'Models',       shortLabel: 'Models',   icon: <Brain size={15} /> },
      { id: 'intelligence', label: 'Intelligence', shortLabel: 'Intel',    icon: <Newspaper size={15} /> },
    ],
  },
  {
    title: 'AI Systems',
    items: [
      { id: 'ai-lab',       label: 'AI Lab',       shortLabel: 'AI Lab',  icon: <Atom size={15} />,   accent: 'purple' },
      { id: 'ai-council',   label: 'AI Council',   shortLabel: 'Council', icon: <Brain size={15} />,  accent: 'purple' },
      { id: 'neural-lab',   label: 'Neural Lab',   shortLabel: 'Neural',  icon: <Zap size={15} />,    accent: 'blue' },
      { id: 'quantum-lab',  label: 'Quantum Lab',  shortLabel: 'Quantum', icon: <Cpu size={15} />,    accent: 'cyan' },
    ],
  },
  {
    title: 'Validation',
    items: [
      { id: 'goal-governor', label: 'Goal Governor', shortLabel: 'Goal',     icon: <Target size={15} />,       accent: 'green' },
      { id: 'policies',      label: 'Policies',      shortLabel: 'Policies', icon: <FlaskConical size={15} />, accent: 'cyan' },
      { id: 'traces',        label: 'Trace Replay',  shortLabel: 'Traces',   icon: <Waypoints size={15} />,    accent: 'yellow' },
    ],
  },
  {
    title: 'Operations',
    items: [
      { id: 'risk',      label: 'Risk',      shortLabel: 'Risk',    icon: <Shield size={15} /> },
      { id: 'execution', label: 'Execution', shortLabel: 'Orders',  icon: <GitBranch size={15} /> },
      { id: 'account',   label: 'Account',   shortLabel: 'Account', icon: <Cpu size={15} /> },
    ],
  },
]

const ALL_NAV = NAV_SECTIONS.flatMap((s) => s.items)
const byId = (id: NavView) => ALL_NAV.find((item) => item.id === id)!
// Bottom tabs: Dashboard, Engine, AI Council, Risk, + More
const BOTTOM_TABS: NavItem[] = [byId('dashboard'), byId('engine'), byId('ai-council'), byId('risk')]

function accentClasses(accent?: string, active = false) {
  if (accent === 'purple') return active ? 'bg-brand-purple/15 text-brand-purple' : 'text-gray-400 hover:text-brand-purple hover:bg-surface-elevated'
  if (accent === 'cyan')   return active ? 'bg-brand-cyan/15 text-brand-cyan'     : 'text-gray-400 hover:text-brand-cyan hover:bg-surface-elevated'
  if (accent === 'green')  return active ? 'bg-brand-green/15 text-brand-green'   : 'text-gray-400 hover:text-brand-green hover:bg-surface-elevated'
  if (accent === 'yellow') return active ? 'bg-brand-yellow/15 text-brand-yellow' : 'text-gray-400 hover:text-brand-yellow hover:bg-surface-elevated'
  if (accent === 'orange') return active ? 'bg-brand-orange/15 text-brand-orange' : 'text-gray-400 hover:text-brand-orange hover:bg-surface-elevated'
  return active ? 'bg-brand-blue/15 text-brand-blue' : 'text-gray-400 hover:text-gray-200 hover:bg-surface-elevated'
}

export function Layout({ children }: { children: ReactNode }) {
  const { send } = useDashboardWs()
  const [drawerOpen, setDrawerOpen] = useState(false)

  const activeView      = useStore((s) => s.activeView)
  const setActiveView   = useStore((s) => s.setActiveView)
  const wsConnected     = useStore((s) => s.wsConnected)
  const runtimeState    = useStore((s) => s.runtimeState)
  const monitoring      = useStore((s) => s.monitoring)
  const aiCouncilStatus = useStore((s) => s.aiCouncilStatus)

  const mode       = runtimeState?.execution_mode ?? '...'
  const killActive = runtimeState?.kill_switch_active ?? false
  const aiEnabled  = aiCouncilStatus?.enabled ?? false
  const aiOnline   = aiCouncilStatus?.gateway_available ?? false

  function toggleKillSwitch() { send({ action: 'kill_switch', active: !killActive }) }
  function navigate(id: NavView) { setActiveView(id); setDrawerOpen(false) }

  const SidebarNav = ({ compact = false }: { compact?: boolean }) => (
    <nav className={clsx('flex-1 overflow-y-auto', compact ? 'px-2 py-2' : 'px-2 py-3')}>
      {NAV_SECTIONS.map((section) => (
        <div key={section.title} className="mb-4">
          <div className="px-3 mb-1.5 text-[10px] font-semibold uppercase tracking-widest text-gray-600">
            {section.title}
          </div>
          <div className="space-y-0.5">
            {section.items.map((item) => {
              const active = activeView === item.id
              return (
                <button
                  key={item.id}
                  onClick={() => navigate(item.id)}
                  className={clsx(
                    'w-full flex items-center gap-2.5 px-3 rounded-md text-sm transition-colors',
                    compact ? 'py-2.5' : 'py-2',
                    accentClasses(item.accent, active),
                  )}
                >
                  {item.icon}
                  <span className="font-medium">{item.label}</span>
                  {active && <ChevronRight size={11} className="ml-auto opacity-60" />}
                  {item.id === 'ai-lab' && aiEnabled && !active && (
                    <span className={clsx(
                      'ml-auto w-1.5 h-1.5 rounded-full',
                      aiOnline ? 'bg-brand-green animate-pulse-slow' : 'bg-brand-yellow',
                    )} />
                  )}
                </button>
              )
            })}
          </div>
        </div>
      ))}
    </nav>
  )

  return (
    <div className="flex h-screen bg-surface overflow-hidden">

      {/* ── DESKTOP SIDEBAR ─────────────────────────────────────────────── */}
      <aside className="hidden md:flex w-56 flex-shrink-0 flex-col border-r border-surface-border bg-surface-card">
        {/* Logo header */}
        <div className="px-4 py-4 border-b border-surface-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-brand-blue/30 to-brand-purple/30 border border-brand-blue/20 flex items-center justify-center">
              <Activity size={15} className="text-brand-blue" />
            </div>
            <div>
              <div className="text-xs font-bold text-gray-100 leading-none tracking-wide">Quantum AI</div>
              <div className="text-[10px] text-gray-500 mt-0.5">Angel One · NSE/BSE/MCX</div>
            </div>
          </div>
        </div>

        <SidebarNav />

        {/* Sidebar footer */}
        <div className="px-3 py-3 border-t border-surface-border space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-gray-600 uppercase tracking-wider">WS</span>
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
            <span className="text-[10px] text-gray-600 uppercase tracking-wider">Mode</span>
            <div className="scale-90 origin-right">{execModeBadge(mode)}</div>
          </div>
          {aiEnabled && (
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-gray-600 uppercase tracking-wider">AI</span>
              <div className="flex items-center gap-1">
                <span className={clsx('w-1.5 h-1.5 rounded-full', aiOnline ? 'bg-brand-green' : 'bg-brand-yellow')} />
                <span className="text-[10px] font-mono text-gray-400">{aiOnline ? 'LIVE' : 'STUB'}</span>
              </div>
            </div>
          )}
          {killActive && (
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Status</span>
              <span className="text-[10px] font-mono text-brand-red font-bold">HALTED</span>
            </div>
          )}
          {monitoring && (
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Orders</span>
              <span className="text-[10px] font-mono text-gray-300">{monitoring.total_orders}</span>
            </div>
          )}
        </div>
      </aside>

      {/* ── MOBILE DRAWER OVERLAY ────────────────────────────────────────── */}
      {drawerOpen && (
        <div
          className="md:hidden fixed inset-0 z-40 bg-black/70 backdrop-blur-sm"
          onClick={() => setDrawerOpen(false)}
        />
      )}
      <aside className={clsx(
        'md:hidden fixed top-0 left-0 h-full w-64 z-50 flex flex-col',
        'bg-surface-card border-r border-surface-border shadow-2xl',
        'transition-transform duration-300 ease-in-out',
        drawerOpen ? 'translate-x-0' : '-translate-x-full',
      )}>
        <div className="px-4 py-4 border-b border-surface-border flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-brand-blue/30 to-brand-purple/30 border border-brand-blue/20 flex items-center justify-center">
              <Activity size={15} className="text-brand-blue" />
            </div>
            <div>
              <div className="text-xs font-bold text-gray-100 leading-none">Quantum AI</div>
              <div className="text-[10px] text-gray-500 mt-0.5">Angel One · NSE/BSE/MCX</div>
            </div>
          </div>
          <button
            onClick={() => setDrawerOpen(false)}
            className="p-1.5 rounded-md text-gray-400 hover:text-gray-200 hover:bg-surface-elevated"
          >
            <X size={16} />
          </button>
        </div>
        <SidebarNav compact />
        <div className="px-4 py-4 border-t border-surface-border space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">Mode</span>
            {execModeBadge(mode)}
          </div>
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

        {/* Topbar header */}
        <header className="h-12 flex-shrink-0 flex items-center justify-between px-4 border-b border-surface-border bg-surface-card">
          <div className="flex items-center gap-3">
            <button
              className="md:hidden p-1 rounded-md text-gray-400 hover:text-gray-200 active:bg-surface-elevated"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open menu"
            >
              <Menu size={18} />
            </button>
            <span className={clsx(
              'w-2 h-2 rounded-full',
              monitoring?.status === 'HEALTHY' ? 'bg-brand-green animate-pulse-slow'
              : monitoring?.status === 'DEGRADED' ? 'bg-brand-yellow animate-pulse-slow'
              : 'bg-brand-red',
            )} />
            <span className="text-xs text-gray-400 font-mono hidden sm:block">
              {monitoring?.status ?? 'CONNECTING'}
            </span>
            <span className="text-xs text-gray-600 font-mono hidden md:block">
              {monitoring?.average_latency_ms != null ? `${monitoring.average_latency_ms.toFixed(1)}ms` : '—'}
            </span>
            {aiEnabled && (
              <div className="hidden lg:flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-surface-elevated border border-surface-border">
                <Atom size={11} className={aiOnline ? 'text-brand-green' : 'text-brand-yellow'} />
                <span className="text-[10px] font-mono text-gray-400">
                  {aiOnline ? (aiCouncilStatus?.gateway_runtime ?? 'live') : 'stub'}
                </span>
              </div>
            )}
          </div>

          <div className="flex items-center gap-2">
            {/* WS indicator (mobile only) */}
            <div className="md:hidden flex items-center gap-1">
              {wsConnected
                ? <Wifi size={12} className="text-brand-green" />
                : <WifiOff size={12} className="text-brand-red" />}
            </div>
            {killActive && (
              <div className="hidden sm:flex items-center gap-1 px-2 py-0.5 rounded bg-brand-red/10 border border-brand-red/30">
                <ShieldCheck size={11} className="text-brand-red" />
                <span className="text-[10px] font-mono text-brand-red">HALTED</span>
              </div>
            )}
            {/* Kill switch button */}
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

        <main className="flex-1 overflow-y-auto p-4 md:p-5 pb-20 md:pb-5 animate-fade-in">
          {children}
        </main>
      </div>

      {/* ── MOBILE BOTTOM TAB BAR ─────────────────────────────────────────── */}
      <nav className="md:hidden fixed bottom-0 left-0 right-0 z-30 bg-surface-card border-t border-surface-border">
        <div className="flex items-center justify-around px-1 py-1">
          {BOTTOM_TABS.map((item) => (
            <button
              key={item.id}
              onClick={() => navigate(item.id)}
              className={clsx(
                'flex flex-col items-center gap-0.5 px-3 py-2 rounded-lg transition-colors min-w-0 flex-1',
                activeView === item.id
                  ? item.accent === 'purple' ? 'text-brand-purple bg-brand-purple/10'
                    : 'text-brand-blue bg-brand-blue/10'
                  : 'text-gray-500 active:bg-surface-elevated',
              )}
            >
              {item.icon}
              <span className="text-[9px] font-medium truncate">{item.shortLabel ?? item.label}</span>
            </button>
          ))}
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
