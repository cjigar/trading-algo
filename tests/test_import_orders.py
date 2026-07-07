"""Order-report importer: normalization + upsert dedup."""

from __future__ import annotations

from algo_trading.persistence.repositories import Repository
from algo_trading.tools.import_orders import import_orders, normalize_order_row


def _row(**kw):
    base = {
        "nOrdNo": "ORD1", "trdSym": "NIFTY2513023000CE", "trnsTp": "B",
        "qty": "75", "fldQty": "75", "avgPrc": "101.5", "prcTp": "L",
        "prod": "NRML", "ordSt": "complete", "ordDtTm": "30-Jan-2025 10:15:04",
    }
    base.update(kw)
    return base


def test_normalize_order():
    f = normalize_order_row(_row())
    assert f["order_id"] == "ORD1"
    assert f["trading_symbol"] == "NIFTY2513023000CE"
    assert f["side"] == "B"
    assert f["quantity"] == 75
    assert f["filled_quantity"] == 75
    assert f["status"] == "complete"


def test_normalize_returns_none_without_id_or_symbol():
    assert normalize_order_row({"foo": 1}) is None
    assert normalize_order_row({"nOrdNo": "X"}) is None  # no symbol


def test_import_upserts_by_order_id(repo: Repository):
    s1 = import_orders(repo, [_row(nOrdNo="A", ordSt="open"), _row(nOrdNo="B")])
    assert s1 == {"inserted": 2, "updated": 0, "unparsed": 0, "total": 2}

    # re-import order A with a new status -> updated, not inserted
    s2 = import_orders(repo, [_row(nOrdNo="A", ordSt="complete", fldQty="75")])
    assert s2["inserted"] == 0 and s2["updated"] == 1

    orders = repo.broker_orders_for_day()
    assert len(orders) == 2  # still 2 distinct orders
    a = next(o for o in orders if o.order_id == "A")
    assert a.status == "complete"  # updated in place


def test_unparsed_counted(repo: Repository):
    s = import_orders(repo, [{"garbage": 1}])
    assert s["unparsed"] == 1 and s["inserted"] == 0
