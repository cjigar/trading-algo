"""Streamlit monitoring & control dashboard.

Observational only: it reads state from SQLite via :class:`StateBridge` and issues control
commands through it. It holds no broker session and places no orders. The trading loop runs in a
separate process. The paper/live indicator is shown prominently so live mode is never mistaken.
"""

from __future__ import annotations

from algo_trading.config.settings import get_settings
from algo_trading.dashboard.state_bridge import StateBridge
from algo_trading.domain.enums import AlgoState
from algo_trading.reporting import summarize_fills


def render() -> None:  # pragma: no cover - requires the Streamlit runtime
    import streamlit as st

    settings = get_settings()
    st.set_page_config(page_title="Algo Trading Dashboard", layout="wide")

    bridge = StateBridge(settings)
    state = bridge.read_state()

    # --- Mode + kill-switch banner ---
    mode = settings.mode.value.upper()
    if settings.live_armed:
        st.error(f"🔴 LIVE TRADING ARMED — real orders. Mode: {mode}")
    else:
        st.info(f"🟢 PAPER MODE — no real orders. Mode: {mode}")

    if state.algo_state is AlgoState.HALTED:
        st.warning("⛔ Algo is HALTED (kill-switch or manual halt). New entries are blocked.")

    # --- Controls ---
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

    # --- P&L metrics ---
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Algo state", state.algo_state.value)
    m2.metric("Realized P&L", f"{state.realized_pnl:,.2f}")
    m3.metric("Unrealized P&L", f"{state.unrealized_pnl:,.2f}")
    m4.metric("Day P&L", f"{state.day_pnl:,.2f}")

    st.caption(f"Daily loss cap: {settings.daily_loss_cap} | Max positions: {settings.max_positions}")

    # --- Today's P&L from fills ---
    st.subheader("Today's P&L (from fills)")
    summary = summarize_fills(state.trades)
    if summary.trade_count:
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Realized P&L (matched)", f"{summary.total_realized:,.2f}")
        p2.metric("Fills", summary.trade_count)
        p3.metric("Buy value", f"{summary.total_buy_value:,.0f}")
        p4.metric("Sell value", f"{summary.total_sell_value:,.0f}")
        st.caption(
            f"{summary.matched_symbols} symbol(s) with matched round-trips; "
            f"{summary.open_symbols} with an open (unmatched) position. "
            "Realized = matched_qty × (avg sell − avg buy) per symbol; open qty is unrealized."
        )
        st.dataframe(
            [
                {
                    "symbol": r.symbol,
                    "buy qty": r.buy_qty,
                    "sell qty": r.sell_qty,
                    "avg buy": float(r.avg_buy),
                    "avg sell": float(r.avg_sell),
                    "net qty": r.net_qty,
                    "realized P&L": float(r.realized_pnl),
                }
                for r in summary.per_symbol
            ],
            use_container_width=True,
        )
    else:
        st.write("No fills today.")

    # --- Positions ---
    st.subheader("Open positions")
    if state.positions:
        st.dataframe(
            [
                {
                    "symbol": p.instrument.trading_symbol,
                    "qty": p.quantity,
                    "avg": float(p.average_price),
                    "ltp": float(p.last_price),
                    "unrealized": float(p.unrealized_pnl),
                }
                for p in state.positions
            ],
            use_container_width=True,
        )
    else:
        st.write("No open positions.")

    # --- Trades ---
    st.subheader("Trades today")
    if state.trades:
        st.dataframe(
            [
                {
                    "time": str(t.timestamp),
                    "symbol": t.instrument.trading_symbol,
                    "side": t.side.value,
                    "qty": t.quantity,
                    "price": float(t.price),
                }
                for t in state.trades
            ],
            use_container_width=True,
        )
    else:
        st.write("No trades yet.")

    # --- Audit / events ---
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
