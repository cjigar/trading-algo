"use client";

import { useCallback, useEffect, useState } from "react";

import { Banner, DataTable, Metric, Tabs } from "@/components/ui";
import { api, clearToken, type BrokerPnL, type Chain, type Order, type Position, type Trade } from "@/lib/api";
import { useStream } from "@/lib/useStream";

const TABS = ["P&L", "Positions", "Orders", "Trades", "Option Chain", "Config"];

export default function Dashboard() {
  const { data, connected } = useStream();
  const [tab, setTab] = useState("P&L");
  const [positions, setPositions] = useState<Position[]>([]);
  const [brokerPositions, setBrokerPositions] = useState<Record<string, unknown>[]>([]);
  const [brokerPnl, setBrokerPnl] = useState<BrokerPnL | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [saveMsg, setSaveMsg] = useState("");
  // Option-chain view: which underlying to show (null = follow today's active), plus its live chain.
  const [chainUnderlying, setChainUnderlying] = useState<string | null>(null);
  const [chainView, setChainView] = useState<Chain | null>(null);

  const refresh = useCallback(async () => {
    const [p, bp, bpnl, o, t, c, ch] = await Promise.all([
      api.positions(), api.brokerPositions(), api.brokerPnl(), api.orders(), api.trades(),
      api.config(), api.chain(chainUnderlying ?? undefined),
    ]);
    setPositions(p);
    setBrokerPositions(bp);
    setBrokerPnl(bpnl);
    setOrders(o);
    setTrades(t);
    setConfig(c);
    setChainView(ch);
  }, [chainUnderlying]);

  useEffect(() => {
    refresh().catch(() => {});
    const id = setInterval(() => refresh().catch(() => {}), 5000);
    return () => clearInterval(id);
  }, [refresh]);

  async function control(cmd: "start" | "stop" | "flatten") {
    if (cmd !== "start" && !confirm(`Confirm ${cmd.toUpperCase()}?`)) return;
    await api.control(cmd);
  }

  async function saveConfig() {
    setSaveMsg("");
    try {
      const updated = await api.saveConfig(config);
      setConfig(updated);
      setSaveMsg("Saved.");
    } catch (e) {
      setSaveMsg(`Error: ${(e as Error).message}`);
    }
  }

  const state = data?.state;
  const pnl = data?.pnl;
  const underlyings = state?.oi_underlyings ?? [];
  const activeUnderlying = state?.active_underlying ?? null;
  const shownUnderlying = chainUnderlying ?? chainView?.underlying ?? activeUnderlying;

  return (
    <main className="mx-auto max-w-6xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Trading Algo</h1>
        <button onClick={() => { clearToken(); location.href = "/login"; }} className="text-sm text-neutral-400">
          Logout
        </button>
      </div>

      {state && <Banner mode={state.mode} liveArmed={state.live_armed} algoState={state.algo_state} strategy={state.strategy} />}

      <div className="flex items-center gap-2">
        <button onClick={() => control("start")} className="rounded-md bg-emerald-700 px-3 py-2 text-sm hover:bg-emerald-600">▶ Start</button>
        <button onClick={() => control("stop")} className="rounded-md bg-red-800 px-3 py-2 text-sm hover:bg-red-700">⏹ Stop</button>
        <button onClick={() => control("flatten")} className="rounded-md bg-neutral-700 px-3 py-2 text-sm hover:bg-neutral-600">🧹 Flatten</button>
        <span className="ml-auto text-xs text-neutral-500">{connected ? "● live" : "○ offline"}</span>
      </div>

      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      {tab === "P&L" && pnl && (
        <div className="space-y-6">
          {brokerPnl && (
            <div className="space-y-2">
              <h2 className="text-sm font-medium text-neutral-300">Broker account P&amp;L (live)</h2>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                <Metric
                  label="Realized P&L (today)"
                  value={<span className={brokerPnl.total_realized >= 0 ? "text-emerald-400" : "text-red-400"}>
                    ₹{brokerPnl.total_realized.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                  </span>}
                />
                <Metric label="Open positions" value={brokerPnl.open_count} />
                <Metric label="Positions (total)" value={brokerPnl.per_position.length} />
              </div>
              <p className="text-xs text-neutral-500">
                Realized on squared (matched) quantity, from the broker snapshot at last reconcile. Open positions&apos; unrealized MTM is not included (needs live LTP).
              </p>
              <BrokerPnLTable pnl={brokerPnl} />
            </div>
          )}
          <div className="space-y-2">
            <h2 className="text-sm font-medium text-neutral-300">Algo session P&amp;L (this session&apos;s fills)</h2>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Metric label="Realized P&L" value={pnl.total_realized.toLocaleString()} />
              <Metric label="Algo state" value={state?.algo_state ?? "—"} />
              <Metric label="Fills" value={pnl.trade_count} />
              <Metric label="Buy / Sell value" value={`${pnl.total_buy_value.toLocaleString()} / ${pnl.total_sell_value.toLocaleString()}`} />
            </div>
            <DataTable rows={pnl.per_symbol as unknown as Record<string, unknown>[]} />
          </div>
        </div>
      )}
      {tab === "Positions" && (
        <div className="space-y-6">
          <div className="space-y-2">
            <h2 className="text-sm font-medium text-neutral-300">Algo positions (this session)</h2>
            <DataTable rows={positions as unknown as Record<string, unknown>[]} />
          </div>
          <div className="space-y-2">
            <h2 className="text-sm font-medium text-neutral-300">
              Broker positions (live account) · {brokerPositions.length}
            </h2>
            <p className="text-xs text-neutral-500">
              Captured from the broker at the algo&apos;s last reconcile (startup). Includes positions opened outside this algo.
            </p>
            <DataTable rows={brokerPositions} />
          </div>
        </div>
      )}
      {tab === "Orders" && <DataTable rows={orders as unknown as Record<string, unknown>[]} />}
      {tab === "Trades" && <DataTable rows={trades as unknown as Record<string, unknown>[]} />}
      {tab === "Option Chain" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-neutral-400">Underlying:</span>
            {underlyings.map((u) => {
              const selected = u === shownUnderlying;
              return (
                <button
                  key={u}
                  onClick={() => setChainUnderlying(u)}
                  className={`rounded-md px-3 py-1.5 text-sm ${selected ? "bg-blue-600 text-white" : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"}`}
                >
                  {u}
                  {u === activeUnderlying && <span className="ml-1 text-xs opacity-70">• today</span>}
                </button>
              );
            })}
            {chainUnderlying && chainUnderlying !== activeUnderlying && (
              <button onClick={() => setChainUnderlying(null)} className="text-xs text-neutral-400 underline">
                follow today ({activeUnderlying ?? "—"})
              </button>
            )}
          </div>
          {chainView ? (
            <>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Metric label="ATM strike" value={chainView.atm ? chainView.atm.toLocaleString() : "—"} />
                <Metric label="Total CE OI" value={chainView.ce_oi_total.toLocaleString()} />
                <Metric label="Total PE OI" value={chainView.pe_oi_total.toLocaleString()} />
                <Metric label="Higher-OI side" value={chainView.selected_side} />
              </div>
              <OptionChainTable chain={chainView} />
            </>
          ) : (
            <p className="text-sm text-neutral-500">No chain data for {shownUnderlying ?? "the selected underlying"} yet.</p>
          )}
        </div>
      )}
      {tab === "Config" && (
        <div className="space-y-3">
          <div className="grid gap-2 md:grid-cols-2">
            {Object.entries(config).map(([k, v]) => (
              <label key={k} className="flex items-center justify-between gap-3 rounded-md border border-neutral-800 px-3 py-2">
                <span className="text-sm text-neutral-400">{k}</span>
                <input
                  className="w-40 rounded bg-neutral-900 px-2 py-1 text-right text-sm"
                  value={Array.isArray(v) ? v.join(",") : String(v)}
                  onChange={(e) =>
                    setConfig((c) => ({ ...c, [k]: Array.isArray(v) ? e.target.value.split(",").map((s) => s.trim()) : e.target.value }))
                  }
                />
              </label>
            ))}
          </div>
          <div className="flex items-center gap-3">
            <button onClick={saveConfig} className="rounded-md bg-blue-600 px-3 py-2 text-sm hover:bg-blue-500">Save config</button>
            {saveMsg && <span className="text-sm text-neutral-400">{saveMsg}</span>}
          </div>
        </div>
      )}
    </main>
  );
}

// Per-position realized P&L, most negative first (matches the API ordering).
function BrokerPnLTable({ pnl }: { pnl: BrokerPnL }) {
  return (
    <div className="overflow-x-auto rounded-md border border-neutral-800">
      <table className="w-full text-right text-sm tabular-nums">
        <thead className="bg-neutral-900 text-xs uppercase text-neutral-400">
          <tr>
            <th className="px-3 py-2 text-left">Symbol</th>
            <th className="px-3 py-2">Net qty</th>
            <th className="px-3 py-2">Avg buy</th>
            <th className="px-3 py-2">Avg sell</th>
            <th className="px-3 py-2">Realized P&amp;L</th>
            <th className="px-3 py-2 text-center">State</th>
          </tr>
        </thead>
        <tbody>
          {pnl.per_position.map((p) => (
            <tr key={p.symbol} className="border-t border-neutral-800/60">
              <td className="px-3 py-1.5 text-left">{p.symbol}</td>
              <td className="px-3 py-1.5">{p.net_qty}</td>
              <td className="px-3 py-1.5">{p.avg_buy.toFixed(2)}</td>
              <td className="px-3 py-1.5">{p.avg_sell.toFixed(2)}</td>
              <td className={`px-3 py-1.5 ${p.realized_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                ₹{p.realized_pnl.toLocaleString(undefined, { maximumFractionDigits: 2 })}
              </td>
              <td className="px-3 py-1.5 text-center">
                {p.is_open
                  ? <span className="rounded bg-yellow-950 px-2 py-0.5 text-xs text-yellow-300">open</span>
                  : <span className="text-xs text-neutral-500">squared</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Classic option-chain layout: calls on the left, strike in the middle, puts on the right.
// Each side shows OI, intraday change-in-OI, and LTP; the OI cell carries a depth bar and the
// ATM strike's row is highlighted.
function OptionChainTable({ chain }: { chain: Chain }) {
  const rows = [...chain.per_strike].sort((a, b) => a.strike - b.strike);
  const maxOi = Math.max(1, ...rows.flatMap((r) => [r.ce_oi, r.pe_oi]));
  const bar = (oi: number, side: "ce" | "pe") => ({
    background: `linear-gradient(${side === "ce" ? "to left" : "to right"}, ${
      side === "ce" ? "rgba(16,185,129,0.18)" : "rgba(239,68,68,0.18)"
    } ${(oi / maxOi) * 100}%, transparent 0)`,
  });
  const chg = (v: number) => (
    <span className={v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-neutral-500"}>
      {v > 0 ? "+" : ""}{v.toLocaleString()}
    </span>
  );
  return (
    <div className="overflow-x-auto rounded-md border border-neutral-800">
      <table className="w-full text-right text-sm tabular-nums">
        <thead className="bg-neutral-900 text-xs uppercase text-neutral-400">
          <tr>
            <th className="px-3 py-2" colSpan={3}>Calls (CE)</th>
            <th className="px-3 py-2 text-center">Strike</th>
            <th className="px-3 py-2 text-left" colSpan={3}>Puts (PE)</th>
          </tr>
          <tr className="text-[10px]">
            <th className="px-3 py-1">OI</th>
            <th className="px-3 py-1">Chg OI</th>
            <th className="px-3 py-1">LTP</th>
            <th className="px-3 py-1 text-center">{chain.atm ? `ATM ${chain.atm.toLocaleString()}` : ""}</th>
            <th className="px-3 py-1 text-left">LTP</th>
            <th className="px-3 py-1 text-left">Chg OI</th>
            <th className="px-3 py-1 text-left">OI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.strike}
              className={`border-t border-neutral-800/60 ${r.is_atm ? "bg-blue-950/60" : ""}`}
            >
              <td className="px-3 py-1.5" style={bar(r.ce_oi, "ce")}>{r.ce_oi.toLocaleString()}</td>
              <td className="px-3 py-1.5">{chg(r.ce_chg_oi)}</td>
              <td className="px-3 py-1.5 text-emerald-400">{r.ce_ltp.toFixed(2)}</td>
              <td className={`px-3 py-1.5 text-center font-semibold ${r.is_atm ? "text-blue-300" : "text-neutral-200"}`}>
                {r.strike.toLocaleString()}{r.is_atm ? " •" : ""}
              </td>
              <td className="px-3 py-1.5 text-left text-red-400">{r.pe_ltp.toFixed(2)}</td>
              <td className="px-3 py-1.5 text-left">{chg(r.pe_chg_oi)}</td>
              <td className="px-3 py-1.5 text-left" style={bar(r.pe_oi, "pe")}>{r.pe_oi.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
