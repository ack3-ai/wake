from types import SimpleNamespace

from wake.development import transactions
from wake.development.transactions import LegacyTransaction
from wake_rs import Chain

INIT = bytes.fromhex("60006000a000")  # LOG0(0, 0), STOP: deploying emits one event


# `tx.events` is a tuple on every engine, so `tx.events == (...)` holds on both.
def test_revm_events_are_a_tuple():
    chain = Chain()
    with chain.connect(accounts=1):
        tx = chain.deploy(INIT, return_tx=True)
        assert type(tx.events) is tuple
        assert len(tx.events) == 1


def test_json_rpc_events_are_a_tuple(monkeypatch):
    tx = LegacyTransaction("0x00", {}, None, bytes, SimpleNamespace())
    tx._tx_receipt = {"logs": []}
    assert tx.events == ()

    event = object()
    monkeypatch.setattr(transactions, "resolve_tx_events", lambda tx: [event])
    tx = LegacyTransaction("0x01", {}, None, bytes, SimpleNamespace())
    tx._tx_receipt = {"logs": [{}]}
    assert tx.events == (event,)
