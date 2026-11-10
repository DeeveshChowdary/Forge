import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ForgeEvent } from "./api";

/** Fetch JSON from the API; `reload()` refetches. Keeps old data while reloading. */
export function useApi<T = any>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const seq = useRef(0);

  const reload = useCallback(async () => {
    if (!path) return;
    const my = ++seq.current;
    setLoading(true);
    try {
      const d = await api.get<T>(path);
      if (my === seq.current) {
        setData(d);
        setError(null);
      }
    } catch (e: any) {
      if (my === seq.current) setError(e.message ?? String(e));
    } finally {
      if (my === seq.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { data, error, loading, reload, setData };
}

type Listener = (e: ForgeEvent) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;
let connected = false;
const statusListeners = new Set<(c: boolean) => void>();

function ensureSource() {
  if (source || typeof EventSource === "undefined") return;
  source = new EventSource("/api/events/stream");
  source.addEventListener("forge", (m) => {
    const e = JSON.parse((m as MessageEvent).data) as ForgeEvent;
    listeners.forEach((l) => l(e));
  });
  source.onopen = () => {
    connected = true;
    statusListeners.forEach((l) => l(true));
  };
  source.onerror = () => {
    connected = false;
    statusListeners.forEach((l) => l(false));
  };
}

/** Subscribe to the global live event stream (single shared SSE connection). */
export function useEvents(handler: Listener, filter?: (e: ForgeEvent) => boolean) {
  const ref = useRef(handler);
  ref.current = handler;
  const fref = useRef(filter);
  fref.current = filter;
  useEffect(() => {
    ensureSource();
    const l: Listener = (e) => {
      if (!fref.current || fref.current(e)) ref.current(e);
    };
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);
}

export function useStreamStatus() {
  const [c, setC] = useState(connected);
  useEffect(() => {
    ensureSource();
    statusListeners.add(setC);
    return () => {
      statusListeners.delete(setC);
    };
  }, []);
  return c;
}

/** Refetch (debounced) whenever a matching live event arrives. */
export function useLiveReload(reload: () => void, filter?: (e: ForgeEvent) => boolean, ms = 400) {
  const timer = useRef<number | undefined>(undefined);
  useEvents(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(reload, ms);
  }, filter);
}

export function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
