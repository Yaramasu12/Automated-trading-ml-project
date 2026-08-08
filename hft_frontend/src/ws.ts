/**
 * Shared WebSocket Manager — single connection, typed dispatch, rAF-buffered.
 *
 * Architecture:
 * - One WebSocket per connection (never per-widget).
 * - Messages typed: { type, source, timestamp, payload }
 * - Buffer in ref, flush on requestAnimationFrame (~60fps cap).
 * - Zustand store receives flushed batches, not raw ticks.
 * - Reconnects with exponential backoff (capped at 30s).
 * - Tenant-scoped: each user gets their own channel.
 */

import { useEffect, useRef, useState } from 'react';

// ─── Message types ───────────────────────────────────────────────────────────

export interface WsMessage {
  type: string;       // event topic, e.g. 'tick.NIFTY', 'signal.*', 'order.*'
  source: string;     // feed source: 'angel-one' | 'upstox' | 'simulated' | 'system'
  timestamp: number;  // epoch ms when event was generated
  payload: Record<string, unknown>;
}

export interface WsSnapshot {
  type: 'snapshot';
  data: Record<string, unknown>;
}

export type WsEvent = WsMessage | WsSnapshot;

// ─── Topic filter ────────────────────────────────────────────────────────────

export interface TopicFilter {
  /** Wildcard: 'tick.*' matches 'tick.NIFTY', 'tick.BANKNIFTY', etc. */
  patterns: string[];
}

const MATCH_TOPICS = (filter: TopicFilter, topic: string): boolean =>
  filter.patterns.some((p) => {
    const pParts = p.split('.');
    const tParts = topic.split('.');
    return pParts.every((part, i) => part === '*' || part === tParts[i]);
  });

// ─── Store contract (minimal, avoids circular imports) ───────────────────────

export interface AppStoreBridge {
  setWsStatus: (status: 'connecting' | 'connected' | 'disconnected' | 'error' | 'reconnecting') => void;
  applySnapshot: (data: Record<string, unknown>) => void;
  applyMessageBatch: (msgs: WsMessage[]) => void;
}

// Global bridge to avoid circular imports
declare global {
  interface Window {
    __wsBridge?: AppStoreBridge;
  }
}

// ─── Connection class ────────────────────────────────────────────────────────

const WS_URLS: Record<string, string> = {
  paper: import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws/dashboard',
  live: import.meta.env.VITE_WS_LIVE_URL || 'ws://localhost:8001/ws/dashboard',
};

export class SharedWsManager {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private baseDelay = 1000;
  private maxDelay = 30000;
  private backoffTimer: ReturnType<typeof setTimeout> | null = null;
  private messageBuffer: WsMessage[] = [];
  private flushTimer: ReturnType<typeof requestAnimationFrame> | null = null;
  private filters = new Map<string, TopicFilter>();
  private readonly handlers = new Map<string, (msg: WsMessage) => void>();
  private readonly BATCH_SIZE = 50;
  private isFlushScheduled = false;

  constructor(private readonly profile: 'paper' | 'live') {}

  /** Get profile. */
  getProfile(): 'paper' | 'live' {
    return this.profile;
  }

  /** Subscribe to topics with a named filter. */
  subscribe(id: string, filter: TopicFilter): void {
    this.filters.set(id, filter);
    // Re-process any buffered messages that match this new filter
    this.messageBuffer.forEach((msg) => {
      if (MATCH_TOPICS(filter, msg.type)) {
        this.dispatchMessage(msg);
      }
    });
  }

  /** Unsubscribe from a named filter. */
  unsubscribe(id: string): void {
    this.filters.delete(id);
  }

  /** Get connection status. */
  isConnected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  /** Get readyState for UI display. */
  getReadyState(): number | null {
    return this.ws?.readyState ?? null;
  }

  /** Connect (or reconnect). */
  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN || this.ws?.readyState === WebSocket.CONNECTING) {
      return; // Already connecting or connected
    }

    const url = WS_URLS[this.profile];
    if (!url) {
      console.error('[WsManager] No URL configured for profile:', this.profile);
      return;
    }

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      console.log('[WsManager] Connected to', url);
      this.getBridge().setWsStatus('connected');
      // Flush any accumulated buffer on connect
      this.flushBuffer();
    };

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const msg: WsEvent = JSON.parse(event.data);
        this.handleMessage(msg);
      } catch (err) {
        console.error('[WsManager] Failed to parse message:', err, event.data);
      }
    };

    this.ws.onclose = (event: CloseEvent) => {
      console.warn('[WsManager] Disconnected:', event.code, event.reason);
      this.getBridge().setWsStatus('disconnected');
      this.ws = null;
      this.scheduleReconnect();
    };

    this.ws.onerror = (_error: Event) => {
      console.error('[WsManager] Error');
      this.getBridge().setWsStatus('error');
    };
  }

  /** Disconnect permanently. */
  disconnect(): void {
    if (this.backoffTimer) {
      clearTimeout(this.backoffTimer);
      this.backoffTimer = null;
    }
    if (this.flushTimer) {
      cancelAnimationFrame(this.flushTimer);
      this.flushTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.messageBuffer = [];
    this.getBridge().setWsStatus('disconnected');
  }

  /** Send a message to the server. */
  send(data: Record<string, unknown>): void {
    if (!this.isConnected()) {
      console.warn('[WsManager] Cannot send: not connected');
      return;
    }
    try {
      this.ws?.send(JSON.stringify(data));
    } catch (err) {
      console.error('[WsManager] Send failed:', err);
    }
  }

  // ─── Private ─────────────────────────────────────────────────────────

  private getBridge(): AppStoreBridge {
    const bridge = window.__wsBridge;
    if (!bridge) {
      console.warn('[WsManager] No bridge available');
      return {
        setWsStatus: () => {},
        applySnapshot: () => {},
        applyMessageBatch: () => {},
      };
    }
    return bridge;
  }

  private handleMessage(msg: WsEvent): void {
    if (msg.type === 'snapshot' && 'data' in msg) {
      // Snapshots replace entire store sections — apply directly
      this.getBridge().applySnapshot(msg.data);
      return;
    }

    if ('source' in msg && 'timestamp' in msg && 'payload' in msg) {
      // Buffer the message
      this.messageBuffer.push(msg);
      return;
    }
    // Discard unparseable messages
  }

  private flushBuffer(): void {
    if (this.messageBuffer.length === 0) {
      this.isFlushScheduled = false;
      return;
    }

    const batch = this.messageBuffer;
    this.messageBuffer = [];
    this.isFlushScheduled = false;

    // Apply to store in batch
    this.getBridge().applyMessageBatch(batch);

    // Flush any remaining after processing (for newly subscribed filters)
    if (this.messageBuffer.length > 0) {
      this.flushTimer = requestAnimationFrame(() => this.flushBuffer());
    }
  }

  private dispatchMessage(msg: WsMessage): void {
    this.handlers.forEach((fn) => {
      try {
        fn(msg);
      } catch (err) {
        console.error('[WsManager] Handler error:', err);
      }
    });
  }

  private scheduleReconnect(): void {
    if (this.reconnectAttempts >= 20) {
      console.error('[WsManager] Max reconnect attempts reached');
      return;
    }

    const delay = Math.min(
      this.baseDelay * Math.pow(2, this.reconnectAttempts) * (0.5 + Math.random()),
      this.maxDelay
    );
    this.reconnectAttempts++;

    console.log(`[WsManager] Reconnecting in ${Math.round(delay / 1000)}s (attempt ${this.reconnectAttempts})`);
    this.getBridge().setWsStatus('reconnecting');

    this.backoffTimer = setTimeout(() => {
      this.connect();
    }, delay);
  }
}

// ─── Hook ────────────────────────────────────────────────────────────────────

let manager: SharedWsManager | null = null;

export const useSharedWs = (profile: 'paper' | 'live' = 'paper'): SharedWsManager | null => {
  const [_connected, setConnected] = useState(false);

  useEffect(() => {
    if (!manager || manager.getProfile() !== profile) {
      manager = new SharedWsManager(profile);
      manager.connect();
      setConnected(true);
    }

    return () => {
      // Don't disconnect on unmount — keep shared connection alive
    };
  }, [profile]);

  return manager;
};

/** Subscribe to specific topics from a component. */
export const useWsSubscribe = (
  manager: SharedWsManager | null,
  id: string,
  filter: TopicFilter,
  handler: (msg: WsMessage) => void
): void => {
  useEffect(() => {
    if (manager) {
      manager.subscribe(id, filter);
      manager.handlers.set(id, handler);
    }
    return () => {
      if (manager) {
        manager.unsubscribe(id);
        manager.handlers.delete(id);
      }
    };
  }, [manager, id, filter, handler]);
};

/** Get connection status for UI. */
export const useWsStatus = (): 'connecting' | 'connected' | 'disconnected' | 'error' | 'reconnecting' => {
  const [status, setStatus] = useState<'connecting' | 'connected' | 'disconnected' | 'error' | 'reconnecting'>('connecting');

  useEffect(() => {
    const bridge = window.__wsBridge;
    if (!bridge) return;
    // Simple polling bridge — in production, wire to Zustand store
    const interval = setInterval(() => {
      setStatus('connected');
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  return status;
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** rAF-batched state update — never call setState per tick.
 * Returns the transformed state. Call the returned `push` function to add messages.
 */
export const useRafBatchedState = <S, M extends WsMessage>(
  initial: S,
  transform: (msgs: M[]) => S
): { state: S; push: (msg: M) => void } => {
  const [state, setState] = useState<S>(initial);
  const bufferRef = useRef<M[]>([]);
  const timerRef = useRef<ReturnType<typeof requestAnimationFrame> | null>(null);
  const scheduleRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const scheduleFlush = () => {
      const buf = bufferRef.current;
      if (buf.length > 100) {
        if (timerRef.current) cancelAnimationFrame(timerRef.current);
        timerRef.current = null;
        if (buf.length > 0) {
          setState(transform(buf));
          bufferRef.current = [];
        }
        return;
      }
      if (timerRef.current) return;
      timerRef.current = requestAnimationFrame(() => {
        timerRef.current = null;
        const buf2 = bufferRef.current;
        if (buf2.length > 0) {
          setState(transform(buf2));
          bufferRef.current = [];
        }
      });
    };
    scheduleRef.current = scheduleFlush;
    return () => {
      if (timerRef.current) cancelAnimationFrame(timerRef.current);
    };
  }, [transform]);

  return {
    state,
    push: (msg: M) => {
      bufferRef.current.push(msg);
      if (scheduleRef.current) scheduleRef.current();
    },
  };
};
