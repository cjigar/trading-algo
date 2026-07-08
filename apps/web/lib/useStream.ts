"use client";

import { useEffect, useState } from "react";

import { apiBase, getToken, type StreamPayload } from "./api";

// Subscribe to the API's SSE live stream. EventSource can't send headers, so the token is passed
// as a query param (the API also accepts it there for the stream route).
export function useStream(): { data: StreamPayload | null; connected: boolean } {
  const [data, setData] = useState<StreamPayload | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const token = getToken();
    if (!token) return;
    const es = new EventSource(`${apiBase()}/api/stream?token=${encodeURIComponent(token)}`);
    es.onopen = () => setConnected(true);
    es.onmessage = (e) => {
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
