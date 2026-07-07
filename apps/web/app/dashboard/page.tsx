"use client";

import { useCallback, useEffect, useState } from "react";

import { Banner, DataTable, Metric, Tabs } from "@/components/ui";
import { api, clearToken, type Order, type Position, type Trade } from "@/lib/api";
import { useStream } from "@/lib/useStream";

const TABS = ["P&L", "Positions", "Orders", "Trades", "Option Chain", "Config"];

export default function Dashboard() {
  const { data, connected } = useStream();
  const [tab, setTab] = useState("P&L");
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [saveMsg, setSaveMsg] = useState("");

  const refresh = useCallback(async () => {
    const [p, o, t, c] = await Promise.all([api.positions(), api.orders(), api.trades(), api.config()]);
    setPositions(p);
    setOrders(o);
    setTrades(t);
    setConfig(c);
  }, []);

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
  const chain = data?.chain;

  return (
    <main className="mx-auto max-w-6xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Trading Algo</h1>
        <button onClick={() => { clearToken(); location.href = "/login"; }} className="text-sm text-neutral-400">
          Logout
        </button>
      </div>

      {state && <Banner mode={state.mode} liveArmed={state.live_armed} algoState={state.algo_state} />}

      <div className="flex items-center gap-2">
        <button onClick={() => control("start")} className="rounded-md bg-emerald-700 px-3 py-2 text-sm hover:bg-emerald-600">▶ Start</button>
        <button onClick={() => control("stop")} className="rounded-md bg-red-800 px-3 py-2 text-sm hover:bg-red-700">⏹ Stop</button>
        <button onClick={() => control("flatten")} className="rounded-md bg-neutral-700 px-3 py-2 text-sm hover:bg-neutral-600">🧹 Flatten</button>
        <span className="ml-auto text-xs text-neutral-500">{connected ? "● live" : "○ offline"}</span>
      </div>

      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      {tab === "P&L" && pnl && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Metric label="Realized P&L" value={pnl.total_realized.toLocaleString()} />
            <Metric label="Algo state" value={state?.algo_state ?? "—"} />
            <Metric label="Fills" value={pnl.trade_count} />
            <Metric label="Buy / Sell value" value={`${pnl.total_buy_value.toLocaleString()} / ${pnl.total_sell_value.toLocaleString()}`} />
          </div>
          <DataTable rows={pnl.per_symbol as unknown as Record<string, unknown>[]} />
        </div>
      )}
      {tab === "Positions" && <DataTable rows={positions as unknown as Record<string, unknown>[]} />}
      {tab === "Orders" && <DataTable rows={orders as unknown as Record<string, unknown>[]} />}
      {tab === "Trades" && <DataTable rows={trades as unknown as Record<string, unknown>[]} />}
      {tab === "Option Chain" && chain && (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-3">
            <Metric label="Total CE OI" value={chain.ce_oi_total.toLocaleString()} />
            <Metric label="Total PE OI" value={chain.pe_oi_total.toLocaleString()} />
            <Metric label="Higher-OI side" value={chain.selected_side} />
          </div>
          <DataTable rows={chain.per_strike as unknown as Record<string, unknown>[]} />
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
