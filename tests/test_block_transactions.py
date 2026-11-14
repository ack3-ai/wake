from types import SimpleNamespace

import pytest

from wake_rs import Chain, HistoryPrunedError


@pytest.fixture
def chain(request, monkeypatch):
    import wake.development.globals as globals_module

    testing = globals_module.get_config().testing.model_copy(
        update={"block_history": getattr(request, "param", 4)}
    )
    monkeypatch.setattr(
        globals_module, "get_config", lambda: SimpleNamespace(testing=testing)
    )
    chain = Chain()
    with chain.connect(accounts=2):
        yield chain


def send(chain):
    return chain.accounts[1].transact(value=1, from_=chain.accounts[0])


def test_empty_and_mined_block_transactions(chain):
    assert chain.blocks["latest"].txs == []

    tx = send(chain)
    assert tx.block.txs == [tx]
    assert tx.block.txs[0] is tx

    # Mutating a returned list must not alter the block's transaction membership.
    tx.block.txs.clear()
    assert tx.block.txs == [tx]

    chain.mine()
    assert chain.blocks["latest"].txs == []
    assert tx.block.txs == [tx]


def test_pending_transactions_follow_their_block_through_mining(chain):
    with chain.change_automine(False):
        pending = chain.blocks["pending"]
        assert pending.txs == []

        first = send(chain)
        assert pending.txs == [first]
        assert first.block.txs == [first]

        second = send(chain)
        assert pending.txs == [first, second]
        assert first.block.txs == [first, second]
        call = chain.accounts[1].call(
            from_=chain.accounts[0], block="pending", return_call=True
        )
        assert call.block.txs == [first, second]

        chain.mine()
        assert first.block == second.block
        assert first.block.txs == [first, second]
        assert pending.txs == [first, second]

        third = send(chain)
        assert chain.blocks["pending"].txs == [third]
        assert pending.txs == [first, second]
        chain.mine()
        assert third.block.txs == [third]


@pytest.mark.parametrize("chain", [1], indirect=True)
def test_pruned_block_transactions_raise_and_retained_blocks_still_resolve(chain):
    old = send(chain).block
    chain.mine()
    old_empty = chain.blocks["latest"]
    for _ in range(8):
        chain.mine()

    for block in (old, old_empty):
        with pytest.raises(HistoryPrunedError, match="no longer retained"):
            block.txs
    assert chain.blocks["latest"].txs == []

    tx = send(chain)
    assert chain.txs.first_index > 0
    assert tx.block.txs == [tx]


@pytest.mark.parametrize("replace", [False, True])
def test_reverted_block_does_not_resolve_replacement_transactions(chain, replace):
    snapshot = chain.snapshot()
    discarded_tx = send(chain)
    discarded = discarded_tx.block
    chain.revert(snapshot)

    if replace:
        replacement_tx = send(chain)
        assert replacement_tx.block_number == discarded.number
        assert replacement_tx.block.txs == [replacement_tx]
    with pytest.raises(HistoryPrunedError, match="discarded.*snapshot revert") as error:
        discarded.txs
    assert "block_history" not in str(error.value)


@pytest.mark.parametrize("chain", [1], indirect=True)
def test_snapshot_restores_block_transactions_after_pruning(chain):
    tx = send(chain)
    block = tx.block
    snapshot = chain.snapshot()
    for _ in range(8):
        send(chain)

    with pytest.raises(HistoryPrunedError):
        block.txs
    chain.revert(snapshot)
    assert chain.blocks["latest"] is block
    assert block.txs == [tx]


def test_pending_view_cannot_follow_a_revert_to_a_reused_block_number(chain):
    with chain.change_automine(False):
        snapshot = chain.snapshot()
        discarded_tx = send(chain)
        pending = chain.blocks["pending"]
        assert pending.txs == [discarded_tx]
        chain.revert(snapshot)

        replacement_tx = send(chain)
        assert replacement_tx.block_number == pending.number
        with pytest.raises(HistoryPrunedError, match="snapshot revert"):
            pending.txs
        assert chain.blocks["pending"].txs == [replacement_tx]
        chain.mine()
        assert replacement_tx.block.txs == [replacement_tx]


def test_pending_transaction_survives_revert_to_shared_history(chain):
    with chain.change_automine(False):
        tx = send(chain)
        pending = tx.block
        snapshot = chain.snapshot()
        send(chain)
        chain.revert(snapshot)

        assert tx.block.txs == [tx]
        assert pending.txs == [tx]
        assert tx.cumulative_gas_used == tx.gas_used
        assert tx.call_trace is not None
        chain.mine()
        assert tx.block.txs == [tx]
        assert pending.txs == [tx]


@pytest.mark.parametrize("send_in_context", [False, True])
def test_pending_call_and_view_survive_snapshot_context(chain, send_in_context):
    with chain.change_automine(False):
        call = chain.accounts[1].call(
            from_=chain.accounts[0], block="pending", return_call=True
        )
        pending = call.block
        number = pending.number
        with chain.snapshot_and_revert():
            if send_in_context:
                send(chain)

        assert call.block.number == number
        assert call.block.txs == []
        assert pending.txs == []
        replacement = send(chain)
        assert call.block.txs == [replacement]
        assert pending.txs == [replacement]


def test_disconnect_discards_block_history_but_keeps_stored_results():
    chain = Chain()
    with chain.connect():
        tx = send(chain)
        block = tx.block
    assert tx.status == 1
    assert tx.raw_return_value == b""
    with pytest.raises(HistoryPrunedError, match="disconnect"):
        block.txs


def test_mined_block_cannot_resolve_transactions_from_a_later_connection():
    chain = Chain()
    with chain.connect():
        old = send(chain).block
    with chain.connect():
        tx = send(chain)
        assert tx.block_number == old.number
        with pytest.raises(HistoryPrunedError, match="disconnect"):
            old.txs
        assert tx.block.txs == [tx]


def test_pending_view_cannot_resolve_transactions_from_a_later_connection():
    chain = Chain()
    with chain.connect():
        pending = chain.blocks["pending"]
    with chain.connect(), chain.change_automine(False):
        tx = send(chain)
        assert tx.block_number == pending.number
        with pytest.raises(HistoryPrunedError, match="previous connection"):
            pending.txs
        assert chain.blocks["pending"].txs == [tx]
