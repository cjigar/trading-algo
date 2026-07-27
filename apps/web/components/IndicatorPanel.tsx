"use client";

import type { IndicatorCell, IndicatorPanel, TrendLabel } from "../lib/api";

const CHIP: Record<TrendLabel, string> = {
  bullish: "bg-green-800 text-green-200",
  bearish: "bg-red-800 text-red-200",
  neutral: "bg-neutral-700 text-neutral-300",
  na: "bg-neutral-800 text-neutral-500",
};

// Indicator display order + how to render each cell's numbers.
const ROWS: { key: string; label: string; fmt: (v: Record<string, number | null>) => string }[] = [
  { key: "ema", label: "EMA 9/21/50", fmt: (v) => `${num(v.ema9)} / ${num(v.ema21)} / ${num(v.ema50)}` },
  { key: "vwap", label: "VWAP", fmt: (v) => num(v.vwap) },
  { key: "rsi", label: "RSI 14", fmt: (v) => num(v.rsi) },
  { key: "macd", label: "MACD", fmt: (v) => `${num(v.macd)} / ${num(v.signal)}` },
  { key: "bollinger", label: "Bollinger", fmt: (v) => `${num(v.upper)} / ${num(v.lower)}` },
  { key: "atr", label: "ATR 14", fmt: (v) => num(v.atr) },
  { key: "supertrend", label: "SuperTrend", fmt: (v) => num(v.line) },
];

function num(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}

function Chip({ cell }: { cell?: IndicatorCell }) {
  const label = cell?.label ?? "na";
  return <span className={`rounded px-1.5 py-0.5 text-xs ${CHIP[label]}`}>{label}</span>;
}

export function IndicatorPanelView({ panel }: { panel: IndicatorPanel }) {
  const tfs = Object.keys(panel.timeframes).sort((a, b) => Number(a) - Number(b));
  const orb = panel.orb.values;
  return (
    <div className="rounded-lg bg-neutral-900 p-4">
      <h2 className="mb-2 text-sm font-semibold text-neutral-200">Nifty Indicators</h2>
      <div className="mb-3 flex items-center gap-3 text-xs text-neutral-400">
        <span>ORB (first 30m):</span>
        <span>High {num(orb.or_high)}</span>
        <span>Low {num(orb.or_low)}</span>
        <span>LTP {num(orb.price)}</span>
        <Chip cell={panel.orb} />
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-neutral-400">
            <th className="text-left font-normal">Indicator</th>
            {tfs.map((tf) => (
              <th key={tf} className="text-right font-normal">{tf}-min</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ROWS.map((row) => (
            <tr key={row.key} className="border-t border-neutral-800">
              <td className="py-1 text-neutral-300">{row.label}</td>
              {tfs.map((tf) => {
                const cell = panel.timeframes[tf]!.cells[row.key];
                return (
                  <td key={tf} className="py-1 text-right">
                    <span className="mr-2 text-neutral-200">{cell ? row.fmt(cell.values) : "—"}</span>
                    <Chip cell={cell} />
                  </td>
                );
              })}
            </tr>
          ))}
          <tr className="border-t border-neutral-700">
            <td className="py-1 font-semibold text-neutral-200">Composite</td>
            {tfs.map((tf) => (
              <td key={tf} className="py-1 text-right">
                <span className="mr-2 text-xs text-neutral-500">{panel.timeframes[tf]!.composite_tally}</span>
                <Chip cell={{ label: panel.timeframes[tf]!.composite, values: {} }} />
              </td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}
