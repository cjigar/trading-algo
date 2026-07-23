"""Shared test fixtures and factories.

Tests run against a real TimescaleDB (`make db-up`); there is no in-process backend.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import pytest

from algo_trading.persistence.testing import SchemaTuning

# Applied before any Settings are constructed: several tests build settings from the environment
# and re-bootstrap the session database (StateBridge, the API app). Without this they would
# re-apply the production 14-day retention policy, whose background job then drops the
# historically-dated snapshots the fixtures write.
os.environ.update(SchemaTuning().as_env())

from algo_trading.domain.enums import (
    ExchangeSegment,
    OptionType,
    OrderType,
    Side,
    Underlying,
    Validity,
)
from algo_trading.domain.models import Instrument, OrderRequest
from algo_trading.persistence.db import create_engine_from_url
from algo_trading.persistence.repositories import Repository
from algo_trading.persistence.testing import (
    create_test_database,
    drop_test_database,
    truncate_all,
    unique_database_name,
)


@pytest.fixture(scope="session")
def pg_engine():
    """One throwaway TimescaleDB database per test session, bootstrapped with the full schema."""
    name = unique_database_name()
    url = create_test_database(name)
    engine = create_engine_from_url(url, settings=SchemaTuning())
    try:
        yield engine
    finally:
        engine.dispose()
        drop_test_database(name)


@pytest.fixture()
def fresh_db(request):
    """URL of an empty database with no schema applied, for tests that bootstrap it themselves
    (schema/policy tests, which must not disturb the session database). Dropped afterwards."""
    name = unique_database_name(f"fresh{abs(hash(request.node.name)) % 10_000}")
    url = create_test_database(name)
    try:
        yield url
    finally:
        drop_test_database(name)


@pytest.fixture()
def engine(pg_engine):
    """Session engine with every table emptied, so tests stay independent."""
    truncate_all(pg_engine)
    return pg_engine


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
