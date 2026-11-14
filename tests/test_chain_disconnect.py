import sys

import pytest

from wake_rs import Chain, HistoryPrunedError


def send(chain):
    return chain.accounts[1].transact(value=1, from_=chain.accounts[0])


def test_pre_snapshot_call_still_replays_within_the_same_connection():
    chain = Chain()
    with chain.connect(accounts=2):
        sender, target = chain.accounts
        target.code = bytes.fromhex("6000545000")  # SLOAD(0), POP, STOP
        call = target.call(from_=sender, block="pending", return_call=True)
        snapshot = chain.snapshot()
        for _ in range(3):
            send(chain)
        chain.revert(snapshot)

        # The database advanced its lineage, but this connection's epoch floor
        # did not move. The point is in the shared prefix and remains replayable.
        assert [slot for slots in call.access_list.values() for slot in slots] == [0]


@pytest.mark.parametrize("reverts", [0, 3])
def test_disconnect_permanently_retires_uncached_replay(reverts):
    chain = Chain()
    with chain.connect(accounts=2):
        sender, target = chain.accounts
        target.code = bytes.fromhex("6000545000")  # SLOAD(0), POP, STOP
        for _ in range(reverts):
            snapshot = chain.snapshot()
            send(chain)
            chain.revert(snapshot)
        call = target.call(from_=sender, block="pending", return_call=True)
        tx = target.transact(from_=sender)

    def check_retired_history():
        for obj, attribute in (
            (call, "access_list"),
            (call, "call_trace"),
            (tx, "call_trace"),
            (tx, "console_logs"),
        ):
            with pytest.raises(HistoryPrunedError, match="disconnect"):
                getattr(obj, attribute)
        assert tx.status == 1
        assert tx.raw_return_value == b""
        assert call.raw_return_value == b""

    check_retired_history()
    with chain.connect(accounts=2):
        sender, target = chain.accounts
        target.code = bytes.fromhex("6001545000")  # SLOAD(1), POP, STOP
        # Reuse the old offsets and block numbers in an unrelated database.
        for _ in range(12):
            send(chain)
        fresh = target.call(from_=sender, block="pending", return_call=True)
        assert [slot for slots in fresh.access_list.values() for slot in slots] == [1]
        check_retired_history()


def test_disconnect_clears_saved_history_containers():
    chain = Chain()
    with chain.connect(accounts=2):
        send(chain)
        txs, blocks = chain.txs, chain.blocks
        chain.snapshot()

    for name in ("txs", "blocks"):
        with pytest.raises(RuntimeError, match="Not connected"):
            getattr(chain, name)
    for lookup in (lambda: txs[0], lambda: blocks[0], lambda: blocks["pending"]):
        with pytest.raises(HistoryPrunedError):
            lookup()

    with chain.connect(accounts=2):
        assert len(chain.txs) == 0
        for lookup in (lambda: txs[0], lambda: blocks[0], lambda: blocks["pending"]):
            with pytest.raises(HistoryPrunedError):
                lookup()


def test_disconnect_keeps_already_computed_results():
    chain = Chain()
    with chain.connect(accounts=2):
        call = chain.accounts[1].call(
            from_=chain.accounts[0], block="pending", return_call=True
        )
        access_list = call.access_list
        tx = send(chain)
        trace = tx.call_trace
    assert call.access_list == access_list
    assert tx.call_trace is trace


def test_repeated_connections_release_chain_owned_history():
    chain = Chain()

    def connect_and_execute():
        with chain.connect(accounts=2):
            chain.snapshot()
            for _ in range(5):
                send(chain)

    connect_and_execute()
    references = sys.getrefcount(chain)
    for _ in range(20):
        connect_and_execute()
    assert sys.getrefcount(chain) == references


@pytest.mark.parametrize("timestamp_change", [None, lambda timestamp: timestamp + 1])
def test_mine_after_disconnect_raises_not_connected(timestamp_change):
    chain = Chain()
    with chain.connect(accounts=1):
        pass

    # A panic would surface as PanicException, which pytest.raises does not catch.
    with pytest.raises(RuntimeError, match="Not connected"):
        chain.mine(timestamp_change)


def test_disconnect_keeps_connection_configuration():
    chain = Chain()
    with chain.connect(accounts=2):
        account = chain.accounts[0]
        account.label = "Alice"
        chain.set_default_accounts(account)

    # Failure reports are rendered after the connection closes.
    assert account.label == "Alice"
    assert len(chain.accounts) == 2
    assert chain.default_tx_account == account


def test_events_first_decoded_after_disconnect():
    chain = Chain()
    with chain.connect(accounts=2):
        sender, target = chain.accounts
        target.code = bytes.fromhex("600160006000a100")  # LOG1(0, 0, topic=1), STOP
        tx = target.transact(from_=sender)

    (event,) = tx.events
    assert event.topics == [(1).to_bytes(32, "big")]
    assert event.data == b""
