"""Entry point for the Streamlit dashboard (a separate process from the trading loop).

Run either of:
    streamlit run src/algo_trading/entrypoints/run_dashboard.py
    algo-dashboard   # console script -> launches the above via streamlit
"""

from __future__ import annotations

import sys

from algo_trading.dashboard.app import render


def main() -> None:  # pragma: no cover - launches the streamlit CLI
    import subprocess

    subprocess.run(["streamlit", "run", __file__, *sys.argv[1:]], check=False)


# `streamlit run <this file>` executes it as __main__; render only in that context so a plain
# import (e.g. the console-script shim or tests) does not touch the Streamlit runtime.
if __name__ == "__main__":  # pragma: no cover
    render()
