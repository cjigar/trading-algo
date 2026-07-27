"use client";

import { useEffect, useRef, useState } from "react";

import { apiBase, getToken, type StreamPayload } from "./api";

// Subscribe to the API's SSE live stream. EventSource can't send headers, so the token is passed
// as a query param (the API also accepts it there for the stream route).
export function useStream(): { data: StreamPayload | null; connected: boolean } {
  const [data, setData] = useState<StreamPayload | null>(null);
  const [connected, setConnected] = useState(false);
  // Last raw frame we committed. A frame byte-identical to the previous one carries no new
  // information, so we skip setData entirely — no re-render, no repaint — keeping the view still
  // whenever the numbers haven't actually moved.
  const lastRaw = useRef<string>("");

  useEffect(() => {
    const token = getToken();
    if (!token) return;
    const es = new EventSource(`${apiBase()}/api/stream?token=${encodeURIComponent(token)}`);
    es.onopen = () => setConnected(true);
    es.onmessage = (e) => {
      if (e.data === lastRaw.current) return;
      lastRaw.current = e.data;
      try {
        setData(JSON.parse(e.data) as StreamPayload);
      } catch {
        /* ignore malformed frame */
      }
    };
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, []);

  return { data, connected };
}
