// Typed fetch helper against the FastAPI backend. Reads the auth token from a cookie and
// attaches it as a Bearer header.

// Resolve the API base at call time. An explicit NEXT_PUBLIC_API_BASE wins; otherwise call the
// API on the SAME host the page was loaded from (port 8000) — so it works on localhost AND any
// LAN IP without a rebuild when the machine's IP changes.
export function apiBase(): string {
  const env = process.env.NEXT_PUBLIC_API_BASE?.trim();
  if (env) return env;
  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  return "http://localhost:8000";
}

export function getToken(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(/(?:^|;\s*)token=([^;]+)/);
  return m ? decodeURIComponent(m[1]!) : null;
}

export function setToken(token: string) {
  // 12h cookie; httpOnly isn't possible from JS, acceptable for a single-user local tool.
  document.cookie = `token=${encodeURIComponent(token)}; path=/; max-age=43200; SameSite=Lax`;
}

export function clearToken() {
  document.cookie = "token=; path=/; max-age=0";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(`${apiBase()}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new Error("Unauthorized");
  }
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return (await res.json()) as T;
}

export const api = {
  get base() { return apiBase(); },
  login: (password: string) =>
    request<{ token: string }>("/api/login", { method: "POST", body: JSON.stringify({ password }) }),
  state: () => request<AlgoState>("/api/state"),
  pnl: () => request<PnL>("/api/pnl"),
  positions: () => request<Position[]>("/api/positions"),
  orders: () => request<Order[]>("/api/orders"),
  trades: () => request<Trade[]>("/api/trades"),
  brokerPositions: () => request<Record<string, unknown>[]>("/api/broker-positions"),
  brokerPnl: () => request<BrokerPnL>("/api/broker-pnl"),
  chain: (underlying?: string) =>
    request<Chain>(`/api/chain${underlying ? `?underlying=${encodeURIComponent(underlying)}` : ""}`),
  config: () => request<Record<string, unknown>>("/api/config"),
  saveConfig: (updates: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/config", { method: "PUT", body: JSON.stringify({ updates }) }),
  control: (command: "start" | "stop" | "flatten") =>
    request<{ ok: boolean; command: string }>(`/api/control/${command}`, { method: "POST" }),
};

// Minimal hand-written types mirroring the API response models. `pnpm gen:types` can regenerate
// a full set from the OpenAPI schema into lib/api-types.ts.
export type AlgoState = {
  mode: string; live_armed: boolean; algo_state: string; strategy: string;
  active_underlying: string | null; oi_underlyings: string[];
};
export type SymbolPnL = {
  symbol: string; buy_qty: number; sell_qty: number; avg_buy: number; avg_sell: number;
  net_qty: number; realized_pnl: number;
};
export type PnL = {
  total_realized: number; total_buy_value: number; total_sell_value: number;
  trade_count: number; matched_symbols: number; open_symbols: number; per_symbol: SymbolPnL[];
};
export type BrokerPositionPnL = {
  symbol: string; net_qty: number; buy_qty: number; sell_qty: number;
  avg_buy: number; avg_sell: number; realized_pnl: number; is_open: boolean;
};
export type BrokerPnL = { total_realized: number; open_count: number; per_position: BrokerPositionPnL[] };
export type Position = {
  symbol: string; side: string; quantity: number; average_price: number; last_price: number;
  unrealized_pnl: number;
};
export type Trade = { time: string; symbol: string; side: string; quantity: number; price: number };
export type Order = {
  order_id: string; symbol: string; side: string; quantity: number; filled_quantity: number;
  price: string; order_type: string; product: string; status: string; order_time: string;
};
export type ChainStrike = {
  strike: number; ce_oi: number; ce_ltp: number; ce_chg_oi: number;
  pe_oi: number; pe_ltp: number; pe_chg_oi: number; is_atm: boolean;
};
export type Chain = {
  underlying: string | null; atm: number | null; ce_oi_total: number; pe_oi_total: number;
  selected_side: string; per_strike: ChainStrike[];
};
export type StreamPayload = { state: AlgoState; pnl: PnL; chain: Chain };
