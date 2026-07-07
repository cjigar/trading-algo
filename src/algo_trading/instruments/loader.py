"""Load and combine scrip masters for the configured underlyings.

In live mode the CSVs are downloaded via the Kotak SDK; otherwise they are read from a local
cache directory (``scrip_cache/<segment>.csv``) so paper mode can run without the broker SDK.
Fails closed with a clear message if no source is available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from algo_trading.config.settings import Settings
from algo_trading.domain.enums import ExchangeSegment, Underlying
from algo_trading.instruments.scrip_master import ScripMaster, ScripMasterError
from algo_trading.observability.logging import get_logger

log = get_logger("instruments.loader")


def combine(masters: list[ScripMaster]) -> ScripMaster:
    instruments = [i for m in masters for i in m.instruments]
    if not instruments:
        raise ScripMasterError("No instruments across scrip masters; failing closed.")
    return ScripMaster(instruments)


def required_segments(settings: Settings) -> list[ExchangeSegment]:
    segs: list[ExchangeSegment] = []
    for u in settings.underlyings:
        seg = ExchangeSegment.for_underlying(u)
        if seg not in segs:
            segs.append(seg)
    return segs


def load_scrip_master(
    settings: Settings,
    neo_client: Any | None = None,
    cache_dir: str | Path = "scrip_cache",
) -> ScripMaster:
    masters: list[ScripMaster] = []
    for seg in required_segments(settings):
        if neo_client is not None:
            masters.append(ScripMaster.download(neo_client, seg))  # pragma: no cover
        else:
            path = Path(cache_dir) / f"{seg.value}.csv"
            if not path.exists():
                raise ScripMasterError(
                    f"No scrip master for {seg.value}: expected {path} in paper mode, or run in "
                    f"live mode to download it. Place a downloaded CSV there, or set ALGO_MODE=live."
                )
            masters.append(ScripMaster.from_csv(path, seg))
    combined = combine(masters)
    log.info("scrip_master_loaded", total=len(combined))
    return combined


def _underlyings_for(settings: Settings) -> list[Underlying]:
    return list(settings.underlyings)
