"""Shared test fixtures and factories."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from algo_trading.domain.enums import (
    ExchangeSegment,
    OptionType,
    OrderType,
    Side,
    Underlying,
    Validity,
)
from algo_trading.domain.models import Instrument, OrderRequest
from algo_trading.persistence.db import create_db_engine
from algo_trading.persistence.repositories import Repository


@pytest.fixture()
def engine(tmp_path):
    return create_db_engine(tmp_path / "test.db")


@pytest.fixture()
def repo(engine):
    return Repository(engine)


def make_instrument(
    underlying: Underlying = Underlying.NIFTY,
    strike: str = "23000",
    option_type: OptionType = OptionType.CE,
    lot_size: int = 75,
) -> Instrument:
    return Instrument(
        underlying=underlying,
        exchange_segment=ExchangeSegment.for_underlying(underlying),
        trading_symbol=f"{underlying.value}25JAN{strike}{option_type.value}",
        instrument_token="11536",
        expiry=date(2025, 1, 30),
        strike=Decimal(strike),
        option_type=option_type,
        lot_size=lot_size,
    )


def make_order_request(
    client_tag: str = "tag-1",
    side: Side = Side.BUY,
    quantity: int = 75,
    price: str = "100.5",
    is_exit: bool = False,
    instrument: Instrument | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_tag=client_tag,
        instrument=instrument or make_instrument(),
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        price=Decimal(price),
        validity=Validity.DAY,
        is_exit=is_exit,
    )


@pytest.fixture()
def instrument_factory():
    return make_instrument


@pytest.fixture()
def order_request_factory():
    return make_order_request


def now() -> datetime:
    return datetime(2025, 1, 15, 10, 0, 0)
