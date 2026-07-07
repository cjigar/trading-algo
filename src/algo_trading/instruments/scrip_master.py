"""Scrip-master ingestion.

Downloads the Kotak Neo scrip-master CSV(s) for the F&O segments and parses them into an
indexed :class:`ScripMaster` table of option :class:`Instrument`s.

Kotak's exact CSV column names vary by segment and are not fully documented, so parsing uses
candidate column-name lists and a pluggable expiry parser. The operator MUST confirm the real
columns against a freshly downloaded file (task 5.1); adjust ``COLUMN_CANDIDATES`` if needed.
The class fails closed: a parse that yields no option rows raises rather than trading blind.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from algo_trading.domain.enums import ExchangeSegment, OptionType, Underlying
from algo_trading.domain.models import Instrument
from algo_trading.observability.logging import get_logger

log = get_logger("instruments.scrip_master")

# our-field -> candidate source column names (checked in order, case-insensitive)
COLUMN_CANDIDATES: dict[str, list[str]] = {
    "trading_symbol": ["pTrdSymbol", "pSymbol", "tradingsymbol", "trading_symbol", "symbol"],
    "instrument_token": ["pSymbol", "instrument_token", "token", "pScripRefKey", "nToken"],
    "underlying": ["pSymbolName", "pDesc", "name", "underlying", "pAsstNm"],
    "expiry": ["pExpiryDate", "expiry", "pExpiry", "expiry_date"],
    "strike": ["dStrikePrice", "strike", "strike_price", "pStrikePrice"],
    "option_type": ["pOptionType", "option_type", "opttype", "pOptType", "instrument_type"],
    "lot_size": ["lLotSize", "lot_size", "lotsize", "pLotSize"],
    "instrument_kind": ["pInstType", "instrument_type", "pGroup", "instrumenttype"],
}


class ScripMasterError(RuntimeError):
    pass


def _pick_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def default_expiry_parser(value: Any) -> date | None:
    """Parse an expiry from ISO string, dd-Mon-yyyy, or an epoch-seconds integer."""
    if value in (None, "", "nan"):
        return None
    text = str(value).strip()
    # numeric -> epoch seconds
    try:
        epoch = int(float(text))
        if epoch > 10_000:  # plausibly seconds since 1970
            return datetime.fromtimestamp(epoch, tz=UTC).date()
    except (ValueError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d%b%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class ScripMaster:
    """An indexed table of tradeable option contracts for one or more segments."""

    def __init__(self, instruments: list[Instrument]) -> None:
        self._instruments = instruments

    def __len__(self) -> int:
        return len(self._instruments)

    @property
    def instruments(self) -> list[Instrument]:
        return list(self._instruments)

    # -- Construction ------------------------------------------------------------------

    @classmethod
    def from_dataframe(
        cls,
        df: pd.DataFrame,
        segment: ExchangeSegment,
        *,
        expiry_parser: Callable[[Any], date | None] = default_expiry_parser,
    ) -> ScripMaster:
        cols = {field: _pick_column(df, cands) for field, cands in COLUMN_CANDIDATES.items()}
        required = ["trading_symbol", "expiry", "strike", "option_type", "lot_size"]
        missing = [f for f in required if cols[f] is None]
        if missing:
            raise ScripMasterError(
                f"Scrip master for {segment.value} missing columns for {missing}. "
                f"Available: {list(df.columns)}. Update COLUMN_CANDIDATES."
            )

        instruments: list[Instrument] = []
        for _, row in df.iterrows():
            opt_raw = str(row[cols["option_type"]]).strip().upper()
            if opt_raw not in ("CE", "PE"):
                continue  # skip futures / non-option rows
            expiry = expiry_parser(row[cols["expiry"]])
            if expiry is None:
                continue
            try:
                strike = Decimal(str(row[cols["strike"]]))
            except (InvalidOperation, ValueError):
                continue
            underlying = cls._infer_underlying(
                str(row[cols["underlying"]]) if cols["underlying"] else "",
                str(row[cols["trading_symbol"]]),
            )
            if underlying is None:
                continue
            token_col = cols["instrument_token"] or cols["trading_symbol"]
            instruments.append(
                Instrument(
                    underlying=underlying,
                    exchange_segment=segment,
                    trading_symbol=str(row[cols["trading_symbol"]]).strip(),
                    instrument_token=str(row[token_col]).strip(),
                    expiry=expiry,
                    strike=strike,
                    option_type=OptionType(opt_raw),
                    lot_size=int(float(row[cols["lot_size"]])),
                )
            )

        if not instruments:
            raise ScripMasterError(f"No option contracts parsed for {segment.value}; failing closed.")
        log.info("scrip_master_parsed", segment=segment.value, count=len(instruments))
        return cls(instruments)

    @classmethod
    def from_csv(cls, path: str | Path, segment: ExchangeSegment) -> ScripMaster:
        df = pd.read_csv(path)
        return cls.from_dataframe(df, segment)

    @classmethod
    def download(cls, neo_client: Any, segment: ExchangeSegment) -> ScripMaster:  # pragma: no cover
        """Download and parse the scrip master via the Kotak SDK. Fails closed on error."""
        try:
            paths = neo_client.scrip_master(exchange_segment=segment.value)
            url = paths[0] if isinstance(paths, list) else paths
            df = pd.read_csv(url)
        except Exception as exc:  # noqa: BLE001
            raise ScripMasterError(f"Scrip master download failed for {segment.value}: {exc}") from exc
        return cls.from_dataframe(df, segment)

    @staticmethod
    def _infer_underlying(name: str, symbol: str) -> Underlying | None:
        blob = f"{name} {symbol}".upper()
        if "SENSEX" in blob:
            return Underlying.SENSEX
        if "NIFTY" in blob and "BANK" not in blob and "FIN" not in blob and "MID" not in blob:
            return Underlying.NIFTY
        return None

    # -- Queries -----------------------------------------------------------------------

    def for_underlying(self, underlying: Underlying) -> list[Instrument]:
        return [i for i in self._instruments if i.underlying is underlying]

    def expiries(self, underlying: Underlying) -> list[date]:
        return sorted({i.expiry for i in self.for_underlying(underlying)})

    def strikes(self, underlying: Underlying, expiry: date, option_type: OptionType) -> list[Decimal]:
        return sorted(
            {
                i.strike
                for i in self._instruments
                if i.underlying is underlying
                and i.expiry == expiry
                and i.option_type is option_type
            }
        )

    def find(
        self,
        underlying: Underlying,
        expiry: date,
        strike: Decimal,
        option_type: OptionType,
    ) -> Instrument | None:
        for i in self._instruments:
            if (
                i.underlying is underlying
                and i.expiry == expiry
                and i.strike == strike
                and i.option_type is option_type
            ):
                return i
        return None
