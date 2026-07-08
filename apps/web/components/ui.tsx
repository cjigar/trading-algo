"use client";

import type { ReactNode } from "react";

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
