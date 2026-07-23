"use client";

import type { ReactNode } from "react";

import type { IndexSpot } from "@/lib/api";

// Sticky bar of live index rates (NIFTY / BANKNIFTY / SENSEX), shown across every tab. Each chip
// carries the spot LTP and the day's change (points + %), green up / red down, with the near-month
// futures LTP on a second line below it. Dimmed when the spot reading is stale (the feed stopped
// publishing); the futures line shows "—" until its own feed ticks or when it goes stale. Renders
// nothing until at least one spot has arrived.
export function SpotTicker({ spots }: { spots: IndexSpot[] }) {
  if (!spots || spots.length === 0) return null;
  const fmt = (n: number) => n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (
    <div className="sticky top-0 z-10 flex flex-wrap items-start gap-6 border-b border-neutral-800 bg-neutral-950/95 px-4 py-2 backdrop-blur">
      {spots.map((s) => {
        const up = s.change >= 0;
        const tone = s.stale ? "text-neutral-500" : up ? "text-emerald-400" : "text-red-400";
        const hasFut = s.fut_ltp != null && !s.fut_stale;
        return (
          <div key={s.underlying} className={`flex flex-col gap-0.5 ${s.stale ? "opacity-60" : ""}`}>
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">{s.underlying}</span>
              <span className="text-lg font-semibold tabular-nums">{fmt(s.ltp)}</span>
              <span className={`text-sm tabular-nums ${tone}`}>
                {up ? "▲" : "▼"} {fmt(Math.abs(s.change))} ({up ? "+" : "-"}{Math.abs(s.change_pct).toFixed(2)}%)
              </span>
              {s.stale && <span className="text-[10px] uppercase text-neutral-500">stale</span>}
            </div>
            <div className="flex items-baseline gap-1.5 text-xs text-neutral-500">
              <span className="uppercase tracking-wide">Fut</span>
              <span className="tabular-nums">{hasFut ? fmt(s.fut_ltp as number) : "—"}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function Banner(
  { mode, liveArmed, algoState, strategy }:
  { mode: string; liveArmed: boolean; algoState: string; strategy?: string },
) {
  return (
    <div className="space-y-2">
      <div
        className={`flex flex-wrap items-center gap-x-2 rounded-md px-4 py-2 font-medium ${
          liveArmed ? "bg-red-950 text-red-300" : "bg-emerald-950 text-emerald-300"
        }`}
      >
        <span>
          {liveArmed ? "🔴 LIVE TRADING ARMED — real orders" : "🟢 PAPER MODE — no real orders"} · Mode: {mode.toUpperCase()}
        </span>
        {strategy && (
          <span className="rounded bg-black/30 px-2 py-0.5 text-xs uppercase tracking-wide">
            Strategy: {strategy}
          </span>
        )}
      </div>
      {algoState === "HALTED" && (
        <div className="rounded-md bg-yellow-950 px-4 py-2 text-yellow-300">
          ⛔ Algo is HALTED — new entries are blocked.
        </div>
      )}
    </div>
  );
}

export function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border border-neutral-800 p-4">
      <div className="text-xs uppercase tracking-wide text-neutral-400">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
    </div>
  );
}

export function Tabs({ tabs, active, onChange }: { tabs: string[]; active: string; onChange: (t: string) => void }) {
  return (
    <div className="flex gap-1 border-b border-neutral-800">
      {tabs.map((t) => (
        <button
          key={t}
          onClick={() => onChange(t)}
          className={`px-4 py-2 text-sm ${
            active === t ? "border-b-2 border-blue-500 text-neutral-100" : "text-neutral-400 hover:text-neutral-200"
          }`}
        >
          {t}
        </button>
      ))}
    </div>
  );
}

export function DataTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (!rows.length) return <p className="py-6 text-neutral-500">No data.</p>;
  const cols = Object.keys(rows[0]!);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 text-left text-neutral-400">
            {cols.map((c) => (
              <th key={c} className="px-3 py-2 font-normal">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-neutral-900">
              {cols.map((c) => (
                <td key={c} className="px-3 py-2">{String(r[c])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
