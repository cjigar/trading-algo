# P&L Page — Smooth Live Updates (no flicker/repaint)

**Goal:** Keep the P&L view updating live on the SSE clock, but make updates land *in place* — no flashing, no scroll jump, no column reflow.

## Problem

Users reported the P&L page "keeps getting refreshed." The page is not reloading — the whole React subtree repaints on a cycle, and scroll jumps. Three contributing causes were found in `apps/web`:

1. **Two out-of-phase update clocks.** The SSE stream (`useStream`) re-renders everything every ~3s, *and* a separate 5s `setInterval` in `dashboard/page.tsx` re-fetched `config` + `chain` and called `setConfig`/`setChainView` on **every tab** — including P&L, which displays neither. The P&L view therefore got forced full re-renders on two unsynchronized timers.
2. **Nothing memoized.** Every frame passed freshly-created arrays to un-memoized tables, so the whole tree reconciled each tick.
3. **Auto-layout table reflow.** Numeric tables used default `table-layout: auto`, so a value changing digit-count recomputed column widths → a visible horizontal "jump."

## Design (implemented)

Keep SSE-driven live updates; remove the *causes of visible motion*.

**One clock, not two** (`dashboard/page.tsx`):
- Dropped the blanket 5s `setInterval`.
- `config` → fetched on mount and when the Config tab opens (`saveConfig` already refreshes from its response). No timer.
- Manual (non-active) chain → fetched, and polled at 5s, **only** while the Option Chain tab is open with a non-active underlying selected. The active chain rides the SSE `data.chain`. Every other tab now re-renders on the single SSE clock alone.

**Skip no-op frames** (`lib/useStream.ts`): a frame byte-identical to the previous one is dropped before `setData`, so quiet periods cause no re-render at all.

**Motionless repaint:**
- `BrokerPnLTable` (the primary live M2M table on P&L) → `table-fixed` with a pinned Symbol column, so live LTP/M2M ticks repaint digits without reflowing columns.
- Generic `DataTable` → `tabular-nums` + `whitespace-nowrap` (constant digit width, single-line rows) — kept auto-layout to avoid wrapping regressions on the variable-width Orders/Trades tabs.
- `OptionChainTable` → left on auto-layout (option LTPs rarely change digit-count; forcing fixed layout would wrap the greeks cells).
- `BrokerPnLTable`, `OptionChainTable`, and `DataTable` wrapped in `React.memo` so untouched subtrees don't re-render.

**Scope:** `apps/web/app/dashboard/page.tsx`, `apps/web/lib/useStream.ts`, `apps/web/components/ui.tsx`. No API/backend changes. Verification: `tsc --noEmit` clean, `next build` succeeds; visual check that P&L numbers tick without flashing, jumping, or scroll collapse.
