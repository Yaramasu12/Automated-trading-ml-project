import { useStore } from './store'
import { Layout } from './components/Layout'
import { Dashboard } from './views/Dashboard'
import { Engine } from './views/Engine'
import { Signals } from './views/Signals'
import { Strategies } from './views/Strategies'
import { Backtest } from './views/Backtest'
import { Models } from './views/Models'
import { Risk } from './views/Risk'
import { Account } from './views/Account'
import { Execution } from './views/Execution'
import { Intelligence } from './views/Intelligence'
import { AILab } from './views/AILab'
import { AICouncil } from './views/AICouncil'
import { NeuralLab } from './views/NeuralLab'
import { QuantumLab } from './views/QuantumLab'
import { Tournament } from './views/Tournament'
import { GoalGovernor } from './views/GoalGovernor'
import { TraceReplay } from './views/TraceReplay'
import { Policies } from './views/Policies'

export function App() {
  const activeView = useStore((s) => s.activeView)
  const error = useStore((s) => s.error)
  const setError = useStore((s) => s.setError)

  function renderView() {
    switch (activeView) {
      case 'dashboard':    return <Dashboard />
      case 'engine':       return <Engine />
      case 'signals':      return <Signals />
      case 'strategies':   return <Strategies />
      case 'backtest':     return <Backtest />
      case 'models':       return <Models />
      case 'risk':         return <Risk />
      case 'execution':    return <Execution />
      case 'intelligence': return <Intelligence />
      case 'ai-lab':       return <AILab />
      case 'ai-council':   return <AICouncil />
      case 'neural-lab':   return <NeuralLab />
      case 'quantum-lab':  return <QuantumLab />
      case 'tournament':   return <Tournament />
      case 'goal-governor': return <GoalGovernor />
      case 'traces':       return <TraceReplay />
      case 'policies':     return <Policies />
      case 'account':      return <Account />
      default:             return <Dashboard />
    }
  }

  return (
    <Layout>
      {error && (
        <div className="mb-4 flex items-center justify-between bg-brand-red/10 border border-brand-red/30 rounded-lg px-4 py-3 text-sm text-brand-red">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="ml-4 text-brand-red/60 hover:text-brand-red">✕</button>
        </div>
      )}
      {renderView()}
    </Layout>
  )
}
