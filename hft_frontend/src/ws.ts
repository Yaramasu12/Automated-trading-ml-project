import { useEffect, useRef } from 'react'
import { useStore } from './store'
import type { WsDashboardMessage } from './types'

const WS_URL = (() => {
  const base = import.meta.env.VITE_WS_URL
  if (base) return base
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const token = import.meta.env.VITE_API_TOKEN ?? ''
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  return `${proto}://${window.location.host}/ws/dashboard${query}`
})()

const RECONNECT_DELAY_MS = 3000

export function useDashboardWs() {
  const applySnapshot = useStore((s) => s.applyWsSnapshot)
  const setConnected = useStore((s) => s.setWsConnected)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const unmountedRef = useRef(false)

  useEffect(() => {
    unmountedRef.current = false

    function connect() {
      if (unmountedRef.current) return
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen = () => {
        if (!unmountedRef.current) setConnected(true)
      }

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string) as WsDashboardMessage
          if (msg.type === 'snapshot') applySnapshot(msg)
        } catch {
          // ignore malformed frames
        }
      }

      ws.onclose = () => {
        if (unmountedRef.current) return
        setConnected(false)
        reconnectRef.current = setTimeout(connect, RECONNECT_DELAY_MS)
      }

      ws.onerror = () => {
        ws.close()
      }
    }

    connect()

    return () => {
      unmountedRef.current = true
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      wsRef.current?.close()
      setConnected(false)
    }
  }, [applySnapshot, setConnected])

  function send(payload: unknown) {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(payload))
    }
  }

  return { send }
}
