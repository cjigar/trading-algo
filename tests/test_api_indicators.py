from datetime import datetime

from apps.api.app.schemas import indicator_panel_out

from algo_trading.analytics.indicators import Cell, IndicatorPanel


def test_indicator_panel_out_serializes():
    panel = IndicatorPanel(
        timeframes={5: {"cells": {"rsi": Cell("bullish", {"rsi": 65.0})},
                        "composite": "bullish", "composite_tally": "1 bull / 0 bear"}},
        orb=Cell("neutral", {"or_high": 105.0, "or_low": 85.0, "price": 95.0}),
        as_of=datetime(2025, 1, 15, 4, 0, 0),
    )
    out = indicator_panel_out(panel)
    dumped = out.model_dump()
    assert dumped["timeframes"]["5"]["composite"] == "bullish"
    assert dumped["timeframes"]["5"]["cells"]["rsi"]["label"] == "bullish"
    assert dumped["orb"]["values"]["or_high"] == 105.0
