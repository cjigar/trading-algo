"""Scrip-master ingestion.

Downloads the Kotak Neo scrip-master CSV(s) for the F&O segments and parses them into an
indexed :class:`ScripMaster` table of option :class:`Instrument`s.

Kotak's exact CSV column names vary by segment and are not fully documented, so parsing uses
candidate column-name lists and a pluggable expiry parser. The operator MUST confirm the real
columns against a freshly downloaded file (task 5.1); adjust ``COLUMN_CANDIDATES`` if needed.
The class fails closed: a parse that yields no option rows raises rather than trading blind.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from algo_trading.domain.enums import ExchangeSegment, OptionType, Underlying
from algo_trading.domain.models import FutureContract, Instrument
from algo_trading.observability.logging import get_logger

log = get_logger("instruments.scrip_master")
_IST = ZoneInfo("Asia/Kolkata")

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


def _extract_csv_url(resp: Any, segment: str) -> str | None:
    """Find the scrip-master CSV URL for ``segment`` in a str / list / nested-dict SDK response."""
    if isinstance(resp, str):
        return resp if ("http" in resp or resp.endswith(".csv")) else None
    if isinstance(resp, list):
        for item in resp:
            url = _extract_csv_url(item, segment)
            if url:
                return url
        return None
    if isinstance(resp, dict):
        # prefer a value whose key or content mentions the segment; else any csv/http url
        candidates: list[str] = []
        for key, val in resp.items():
            if isinstance(val, str) and ("http" in val or val.endswith(".csv")):
                if segment.lower() in val.lower() or segment.lower() in str(key).lower():
                    return val
                candidates.append(val)
            else:
                nested = _extract_csv_url(val, segment)
                if nested:
                    return nested
        return candidates[0] if candidates else None
    return None


def _norm_col(name: str) -> str:
    """Normalize a column name for matching. Kotak's scrip CSV has messy headers with trailing
    spaces and semicolons (e.g. ``dStrikePrice;``, ``lExpiryDate ``), so strip whitespace/semicolons."""
    return re.sub(r"[\s;]", "", str(name)).lower()


def _pick_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    normalized = {_norm_col(c): c for c in df.columns}
    for cand in candidates:
        if _norm_col(cand) in normalized:
            return normalized[_norm_col(cand)]
    return None


# Kotak scrip-master expiries are seconds since 1980-01-01 (the NSE NNF epoch), not the Unix epoch.
_NNF_EPOCH = datetime(1980, 1, 1, tzinfo=UTC)


def default_expiry_parser(value: Any) -> date | None:
    """Parse an expiry from an ISO/dd-Mon-yyyy string, or Kotak's seconds-since-1980 integer."""
    if value in (None, "", "nan"):
        return None
    text = str(value).strip()
    # numeric -> seconds since 1980-01-01 (Kotak NNF epoch)
    try:
        epoch = int(float(text))
        if epoch > 10_000:
            return (_NNF_EPOCH + timedelta(seconds=epoch)).date()
    except (ValueError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d%b%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class ScripMaster:
    """An indexed table of tradeable option contracts (and index futures) for one or more
    segments. Options drive trading; futures are parsed for display only (the rate ticker)."""

    def __init__(
        self, instruments: list[Instrument], futures: list[FutureContract] | None = None
    ) -> None:
        self._instruments = instruments
        self._futures = futures or []

    def __len__(self) -> int:
        return len(self._instruments)

    @property
    def instruments(self) -> list[Instrument]:
        return list(self._instruments)

    @property
    def futures(self) -> list[FutureContract]:
        return list(self._futures)

    # -- Construction ------------------------------------------------------------------

    @classmethod
    def from_dataframe(
        cls,
        df: pd.DataFrame,
        segment: ExchangeSegment,
        *,
        expiry_parser: Callable[[Any], date | None] = default_expiry_parser,
        strike_scale: Decimal = Decimal("1"),
    ) -> ScripMaster:
        """Parse a scrip-master frame. ``strike_scale`` converts the raw strike to rupees — Kotak's
        ``dStrikePrice`` is in paise, so real downloads pass 0.01 (see ``download``/``from_csv``)."""
        cols = {field: _pick_column(df, cands) for field, cands in COLUMN_CANDIDATES.items()}
        required = ["trading_symbol", "expiry", "strike", "option_type", "lot_size"]
        missing = [f for f in required if cols[f] is None]
        if missing:
            raise ScripMasterError(
                f"Scrip master for {segment.value} missing columns for {missing}. "
                f"Available: {list(df.columns)}. Update COLUMN_CANDIDATES."
            )

        token_col = cols["instrument_token"] or cols["trading_symbol"]
        kind_col = cols.get("instrument_kind")

        instruments: list[Instrument] = []
        futures: list[FutureContract] = []
        for _, row in df.iterrows():
            symbol = str(row[cols["trading_symbol"]]).strip()
            underlying = cls._infer_underlying(
                str(row[cols["underlying"]]) if cols["underlying"] else "", symbol
            )
            if underlying is None:
                continue
            expiry = expiry_parser(row[cols["expiry"]])
            if expiry is None:
                continue
            token = str(row[token_col]).strip()
            try:
                lot_size = int(float(row[cols["lot_size"]]))
            except (ValueError, TypeError):
                continue

            opt_raw = str(row[cols["option_type"]]).strip().upper()
            if opt_raw in ("CE", "PE"):
                try:
                    strike = Decimal(str(row[cols["strike"]])) * strike_scale
                except (InvalidOperation, ValueError):
                    continue
                instruments.append(
                    Instrument(
                        underlying=underlying, exchange_segment=segment, trading_symbol=symbol,
                        instrument_token=token, expiry=expiry, strike=strike,
                        option_type=OptionType(opt_raw), lot_size=lot_size,
                    )
                )
            elif cls._is_future_row(kind_col, row, opt_raw, symbol):
                futures.append(
                    FutureContract(
                        underlying=underlying, exchange_segment=segment, trading_symbol=symbol,
                        instrument_token=token, expiry=expiry, lot_size=lot_size,
                    )
                )

        # Fail closed on options only — futures are display-only and their absence must never
        # block trading.
        if not instruments:
            raise ScripMasterError(f"No option contracts parsed for {segment.value}; failing closed.")
        log.info("scrip_master_parsed", segment=segment.value,
                 count=len(instruments), futures=len(futures))
        return cls(instruments, futures)

    @staticmethod
    def _is_future_row(kind_col: str | None, row: Any, opt_raw: str, symbol: str) -> bool:
        """A futures row: an instrument-kind or symbol that names a future. The broker uses
        ``FUTIDX`` for index futures; some feeds mark the option-type column ``XX``/``FUT``."""
        kind = str(row[kind_col]).strip().upper() if kind_col is not None else ""
        return "FUT" in kind or "FUT" in opt_raw or "FUT" in symbol.upper()

    # Kotak scrip-master strikes are in paise; scale to rupees for real downloads.
    KOTAK_STRIKE_SCALE = Decimal("0.01")

    @classmethod
    def from_csv(cls, path: str | Path, segment: ExchangeSegment) -> ScripMaster:
        df = pd.read_csv(path)
        return cls.from_dataframe(df, segment, strike_scale=cls.KOTAK_STRIKE_SCALE)

    @classmethod
    def download(cls, neo_client: Any, segment: ExchangeSegment) -> ScripMaster:  # pragma: no cover
        """Download and parse the scrip master via the Kotak SDK. Fails closed on error.

        ``scrip_master`` may return a URL string, a list of URLs, or a dict keyed by segment; we
        extract the CSV URL for ``segment`` from any of these shapes.
        """
        try:
            resp = neo_client.scrip_master(exchange_segment=segment.value)
            url = _extract_csv_url(resp, segment.value)
            if url is None:
                raise ScripMasterError(
                    f"No CSV URL in scrip_master response for {segment.value}: {str(resp)[:300]}"
                )
            df = pd.read_csv(url)
        except ScripMasterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ScripMasterError(f"Scrip master download failed for {segment.value}: {exc}") from exc
        return cls.from_dataframe(df, segment, strike_scale=cls.KOTAK_STRIKE_SCALE)

    # Index-name fragments that are NOT plain NIFTY/SENSEX and must be excluded.
    # Other NSE "NIFTY *" indices we do NOT trade/display, so a bare "NIFTY" match must exclude
    # them. BANK is deliberately absent — BANKNIFTY is matched first, before this check.
    _NIFTY_EXCLUDE = ("FIN", "MID", "NXT", "NEXT")
    # BSE "SENSEX 50" / "SNSX50" is a distinct index; "SENSEX50" contains "SENSEX" as a substring,
    # so a bare "SENSEX" match must exclude it (same trap as BANKNIFTY vs NIFTY).
    _SENSEX_EXCLUDE = ("SENSEX50", "SNSX50")

    @classmethod
    def _infer_underlying(cls, name: str, symbol: str) -> Underlying | None:
        blob = f"{name} {symbol}".upper()
        if "BANKEX" in blob:
            return None  # BSE BANKEX, not SENSEX
        if "SENSEX" in blob and not any(x in blob for x in cls._SENSEX_EXCLUDE):
            return Underlying.SENSEX
        # BankNifty before plain NIFTY: "BANKNIFTY" / "NIFTY BANK" both contain "NIFTY".
        if "BANKNIFTY" in blob or "NIFTY BANK" in blob:
            return Underlying.BANKNIFTY
        if "NIFTY" in blob and not any(x in blob for x in cls._NIFTY_EXCLUDE):
            return Underlying.NIFTY
        return None

    # -- Queries -----------------------------------------------------------------------

    def for_underlying(self, underlying: Underlying) -> list[Instrument]:
        return [i for i in self._instruments if i.underlying is underlying]

    def near_month_future(
        self, underlying: Underlying, today: date | None = None
    ) -> FutureContract | None:
        """The nearest non-expired futures contract for an underlying (None if none parsed).

        Picks the smallest expiry >= today, so it rolls to the next contract automatically once
        the front month expires. ``today`` defaults to the current IST trading date."""
        ref = today or datetime.now(_IST).date()
        candidates = [
            f for f in self._futures if f.underlying is underlying and f.expiry >= ref
        ]
        return min(candidates, key=lambda f: f.expiry) if candidates else None

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
