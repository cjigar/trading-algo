"""Streamlit monitoring & control dashboard (tabbed).

Observational only: it reads state from the shared DB via :class:`StateBridge` and issues control
commands through it. It holds no broker session and places no orders. The trading loop runs in a
separate process. The paper/live indicator is shown prominently so live mode is never mistaken.

Layout: a static banner + control buttons on top, then a data section (auto-refreshing every N
seconds via ``st.fragment``) organized into tabs — P&L, Orders, Positions, Trades, Audit.
"""

from __future__ import annotations

from algo_trading.config.settings import get_settings
from algo_trading.dashboard.state_bridge import StateBridge
from algo_trading.domain.enums import AlgoState
from algo_trading.reporting import summarize_fills


def render() -> None:  # pragma: no cover - requires the Streamlit runtime
    from datetime import datetime

    import streamlit as st

    settings = get_settings()
    st.set_page_config(page_title="Algo Trading Dashboard", layout="wide")
    bridge = StateBridge(settings)

    # --- Mode banner (static per process) ---
    mode = settings.mode.value.upper()
    if settings.live_armed:
        st.error(f"🔴 LIVE TRADING ARMED — real orders. Mode: {mode}")
    else:
        st.info(f"🟢 PAPER MODE — no real orders. Mode: {mode}")

    # --- Controls (outside the auto-refresh fragment so they stay responsive) ---
    c1, c2, c3, _ = st.columns(4)
    if c1.button("▶ Start", use_container_width=True):
        bridge.send_start()
        st.rerun()
    if c2.button("⏹ Stop (halt)", use_container_width=True):
        bridge.send_stop()
        st.rerun()
    if c3.button("🧹 Flatten all", use_container_width=True):
        bridge.send_flatten()
        st.rerun()

    refresh = max(5, settings.dashboard_refresh_seconds)

    # Status line refreshes on the timer (its own fragment).
    @st.fragment(run_every=refresh)
    def status_line() -> None:
        state = bridge.read_state()
        st.caption(
            f"🔄 Auto-refreshing every {refresh}s · last updated {datetime.now():%H:%M:%S}"
        )
        if state.algo_state is AlgoState.HALTED:
            st.warning("⛔ Algo is HALTED (kill-switch or manual halt). New entries are blocked.")

    status_line()

    # Tabs are created OUTSIDE the fragments so the selected tab persists across auto-refreshes.
    # Each tab holds its own fragment that re-reads and re-renders on the timer.
    tabs = st.tabs(["📊 P&L", "📋 Orders", "📈 Positions", "🧾 Trades", "📜 Audit"])

    @st.fragment(run_every=refresh)
    def pnl_frag() -> None:
        _render_pnl(st, bridge.read_state(), settings)

    @st.fragment(run_every=refresh)
    def orders_frag() -> None:
        _render_orders(st, bridge.read_state())

    @st.fragment(run_every=refresh)
    def positions_frag() -> None:
        _render_positions(st, bridge.read_state())

    @st.fragment(run_every=refresh)
    def trades_frag() -> None:
        _render_trades(st, bridge.read_state())

    @st.fragment(run_every=refresh)
    def audit_frag() -> None:
        _render_audit(st, bridge.read_state())

    with tabs[0]:
        pnl_frag()
    with tabs[1]:
        orders_frag()
    with tabs[2]:
        positions_frag()
    with tabs[3]:
        trades_frag()
    with tabs[4]:
        audit_frag()


def _render_pnl(st, state, settings) -> None:  # pragma: no cover
    st.subheader("Today's P&L (from fills)")
    summary = summarize_fills(state.trades)
    if not summary.trade_count:
        st.write("No fills today.")
        return
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Realized P&L (matched)", f"{summary.total_realized:,.2f}")
    p2.metric("Algo state", state.algo_state.value)
    p3.metric("Buy value", f"{summary.total_buy_value:,.0f}")
    p4.metric("Sell value", f"{summary.total_sell_value:,.0f}")
    st.caption(
        f"Fills: {summary.trade_count} | Daily loss cap: {settings.daily_loss_cap} | "
        f"Max positions: {settings.max_positions}"
    )
    st.caption(
        f"{summary.matched_symbols} symbol(s) with matched round-trips; "
        f"{summary.open_symbols} with an open (unmatched) position. "
        "Realized = matched_qty × (avg sell − avg buy) per symbol; open qty is unrealized."
    )
    st.dataframe(
        [
            {
                "symbol": r.symbol, "buy qty": r.buy_qty, "sell qty": r.sell_qty,
                "avg buy": float(r.avg_buy), "avg sell": float(r.avg_sell),
                "net qty": r.net_qty, "realized P&L": float(r.realized_pnl),
            }
            for r in summary.per_symbol
        ],
        use_container_width=True,
    )


def _render_orders(st, state) -> None:  # pragma: no cover
    st.subheader("Orders (order book)")
    if state.orders:
        st.dataframe(
            [
                {
                    "order id": o.order_id, "symbol": o.trading_symbol, "side": o.side,
                    "qty": o.quantity, "filled": o.filled_quantity, "price": o.price,
                    "type": o.order_type, "product": o.product, "status": o.status,
                    "time": o.order_time,
                }
                for o in state.orders
            ],
            use_container_width=True,
        )
    else:
        st.write("No orders. Run `import-orders` to pull today's order book, or start the loop.")


def _render_positions(st, state) -> None:  # pragma: no cover
    st.subheader("Open positions")
    if state.positions:
        st.dataframe(
            [
                {
                    "symbol": p.instrument.trading_symbol, "qty": p.quantity,
                    "avg": float(p.average_price), "ltp": float(p.last_price),
                    "unrealized": float(p.unrealized_pnl),
                }
                for p in state.positions
            ],
            use_container_width=True,
        )
    else:
        st.write("No open positions.")


def _render_trades(st, state) -> None:  # pragma: no cover
    st.subheader("Trades today (fills)")
    if state.trades:
        st.dataframe(
            [
                {
                    "time": str(t.timestamp), "symbol": t.instrument.trading_symbol,
                    "side": t.side.value, "qty": t.quantity, "price": float(t.price),
                }
                for t in state.trades
            ],
            use_container_width=True,
        )
    else:
        st.write("No trades yet.")


def _render_audit(st, state) -> None:  # pragma: no cover
    st.subheader("Audit events")
    if state.audit:
        st.dataframe(
            [
                {"time": str(a.timestamp), "type": a.event_type, "message": a.message}
                for a in state.audit
            ],
            use_container_width=True,
        )
    else:
        st.write("No audit events.")


if __name__ == "__main__":  # pragma: no cover
    render()
