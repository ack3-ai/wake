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
