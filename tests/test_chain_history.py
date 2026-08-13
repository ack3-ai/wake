"""Chain history retention: the window, the journal floor, and what survives a revert.

`Chain` used to retain every transaction, block and journal entry for its
lifetime, so memory was linear in transactions executed - ~2.1 KB for the
cheapest possible transfer, with no plateau. Retention is now bounded by a
window of the newest `testing.block_history` blocks, and the journal is compacted
to the lowest index those blocks still need.

Asserted findings:

* Retention is flat past the window: RSS stops growing, and the transaction
  count keeps rising, so nothing is silently dropped from the count.
* Blocks are the unit of retention - a transaction is kept exactly as long as
  the block containing it. With automine off one block holds many transactions,
  so the window keeps many more of them, but retention still plateaus at
  `testing.block_history` blocks' worth instead of growing with the run.
* `blocks["latest"]` always resolves, and negative indexing is unaffected while
  the newest transaction's block is retained - that is the dominant usage. Indices
  below the window raise `HistoryPrunedError`, while indices that never existed
  still raise `IndexError`.
* A transaction that has left the window keeps its metadata (`from_`, `status`,
  `events`, `return_value`) and loses only replay (`call_trace`,
  `console_logs`, `access_list`). An *already computed* trace is cached on the
  transaction and deliberately keeps working.
* Reverting to a snapshot restores the same visible history depth it had at
  snapshot time, and blocks in the restored window still resolve.
* `BLOCKHASH` reaches a fixed 256 blocks back, independent of how much metadata
  the chain keeps, and every live snapshot protects the hash range needed by its
  whole restorable block window.
"""

import ctypes
import gc
import platform
from types import SimpleNamespace

import pytest

from wake_rs import Chain, HistoryPrunedError

BLOCK_HISTORY = 256

# calldata word 0 -> block number; returns BLOCKHASH(n)
BLOCKHASH_CODE = bytes.fromhex("6000354060005260206000f3")


@pytest.fixture
def chain(request, monkeypatch):
    import wake.development.globals as globals_module

    block_history = getattr(request, "param", BLOCK_HISTORY)
    testing = globals_module.get_config().testing.model_copy(
        update={"block_history": block_history}
    )
    monkeypatch.setattr(
        globals_module, "get_config", lambda: SimpleNamespace(testing=testing)
    )

    chain = Chain()
    with chain.connect(accounts=3):
        for account in chain.accounts:
            account.balance = 10**21
        yield chain


def transfer(chain, n=1):
    sender, recipient = chain.accounts[0], chain.accounts[1]
    for _ in range(n):
        recipient.transact(value=1, from_=sender)


def replays(tx):
    """Whether the transaction can still be re-executed.

    There is deliberately no `tx.replayable` predicate: a public mirror of the
    gate is one more thing to keep honest, and the gate lives where it is used.
    """
    try:
        tx.call_trace
        return True
    except HistoryPrunedError:
        return False


def tx_depth(chain):
    return len(chain.txs) - chain.txs.first_index


def block_depth(chain):
    return chain.blocks["latest"].number - chain.blocks.first_number + 1


def rss():
    gc.collect()
    # Return the pages to the OS first: otherwise freed history shows up as
    # retained, and the plateau tests would pass for the wrong reason.
    ctypes.CDLL("libc.so.6").malloc_trim(0)
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * 4096


def test_window_bounds_retention_without_losing_the_count(chain):
    transfer(chain, 1000)

    assert len(chain.txs) == 1000
    assert chain.txs.last_index == 999
    # Hysteresis: drained back to `testing.block_history`, allowed to overshoot by an
    # eighth before the next drain.
    assert BLOCK_HISTORY <= block_depth(chain) <= BLOCK_HISTORY + BLOCK_HISTORY // 8
    # Automine puts one transaction in each block, so the transaction window
    # tracks the block window one-for-one.
    assert tx_depth(chain) == block_depth(chain)


def test_newest_transaction_and_latest_block_always_resolve(chain):
    latest = chain.blocks["latest"].number
    transfer(chain, 1000)

    newest = chain.txs[-1]
    assert chain.txs[999] is newest
    assert newest.call_trace is not None
    assert chain.blocks["latest"].number == latest + 1000


def test_pruned_index_raises_history_pruned_but_unknown_index_raises_index_error(chain):
    transfer(chain, 1000)

    with pytest.raises(HistoryPrunedError):
        chain.txs[0]
    with pytest.raises(HistoryPrunedError):
        chain.blocks[0]
    with pytest.raises(IndexError):
        chain.txs[len(chain.txs) + 10]


def test_transaction_outside_the_window_keeps_metadata_and_loses_replay(chain):
    transfer(chain, 1)
    tx = chain.txs[-1]
    sender, status = tx.from_, tx.status

    transfer(chain, 2 * BLOCK_HISTORY + 10)

    assert tx.from_ == sender
    assert tx.status == status
    assert tx.events is not None
    with pytest.raises(HistoryPrunedError, match="pruned"):
        tx.call_trace


def test_a_computed_trace_survives_pruning(chain):
    transfer(chain, 1)
    tx = chain.txs[-1]
    trace = tx.call_trace

    transfer(chain, 2 * BLOCK_HISTORY + 10)

    assert tx.call_trace is trace


@pytest.mark.parametrize("chain", [4], indirect=True)
def test_transactions_are_kept_as_long_as_their_block(chain):
    # Retention is counted in blocks, not transactions. With automine off a block
    # holds many transactions, so the window keeps far more than
    # `testing.block_history` of them - deliberately, because a transaction whose
    # block is retained has to stay reachable. What it must not do is keep
    # transactions from blocks that are already gone.
    per_block = 20
    with chain.change_automine(False):
        for _ in range(40):
            transfer(chain, per_block)
            chain.mine()

    assert block_depth(chain) <= 4 + 1
    assert tx_depth(chain) == block_depth(chain) * per_block
    # The oldest retained transaction is the first one of the oldest retained
    # block, and it is still replayable.
    assert replays(chain.txs[chain.txs.first_index])
    assert replays(chain.txs[-1])
    # Nothing from a dropped block survived.
    with pytest.raises(HistoryPrunedError):
        chain.txs[chain.txs.first_index - 1]


@pytest.mark.parametrize("chain", [5], indirect=True)
def test_transaction_cutoff_is_exact_for_variable_sized_blocks(chain):
    # The transaction count is never chosen, it is derived from the newest dropped
    # block's journal index. Uniform blocks would hide an off-by-one, so vary the
    # sizes and include empty blocks (whose journal index equals their
    # predecessor's).
    import random

    rng = random.Random(7)
    sizes = {}
    with chain.change_automine(False):
        for _ in range(60):
            count = rng.choice([0, 1, 3, 7, 12])
            transfer(chain, count)
            chain.mine()
            sizes[chain.blocks["latest"].number] = count

    first, last = chain.blocks.first_number, chain.blocks["latest"].number
    expected = sum(sizes.get(number, 0) for number in range(first, last + 1))
    assert tx_depth(chain) == expected
    assert replays(chain.txs[chain.txs.first_index])
    with pytest.raises(HistoryPrunedError):
        chain.txs[chain.txs.first_index - 1]


def test_revert_restores_the_same_visible_depth_and_resolves_blocks(chain):
    transfer(chain, 1000)
    depth = tx_depth(chain)
    latest = chain.blocks["latest"].number

    snapshot = chain.snapshot()
    transfer(chain, 500)
    chain.revert(snapshot)

    assert chain.blocks["latest"].number == latest
    assert tx_depth(chain) == depth
    # The block reverted to still resolves, and so does its hash: `BLOCKHASH`
    # would otherwise fall through to the underlying database and answer with
    # something unrelated.
    assert chain.blocks[latest].number == latest
    assert chain.blocks[latest].hash == chain.blocks["latest"].hash
    # Metadata of pre-revert transactions survives...
    assert chain.txs[-1].from_ is not None
    # ...and transactions executed after the revert are replayable again.
    transfer(chain, 5)
    assert chain.txs[-1].call_trace is not None


def test_revert_without_an_intervening_prune_keeps_the_window(chain):
    # The window is frozen lazily, on the first prune that outlives the snapshot,
    # so a snapshot reverted before anything is pruned has nothing frozen and the
    # revert falls back to truncating the live window. That is the path a
    # revert/replay loop takes, so it needs to restore history just as exactly.
    transfer(chain, 10)
    latest = chain.blocks["latest"].number
    total = len(chain.txs)
    assert chain.blocks.first_number == 0, "expected no pruning yet"

    snapshot = chain.snapshot()
    transfer(chain, 10)
    chain.revert(snapshot)

    assert chain.blocks["latest"].number == latest
    assert len(chain.txs) == total
    assert chain.blocks.first_number == 0
    assert chain.blocks[0].number == 0
    assert replays(chain.txs[0])
    assert replays(chain.txs[-1])


def test_nested_snapshots_revert_in_order(chain):
    transfer(chain, 600)
    latest = chain.blocks["latest"].number

    first = chain.snapshot()
    transfer(chain, 300)
    second = chain.snapshot()
    transfer(chain, 300)

    chain.revert(second)
    assert chain.blocks["latest"].number == latest + 300
    chain.revert(first)
    assert chain.blocks["latest"].number == latest


def test_reverting_below_the_pruned_window_does_not_desynchronize_blocks(chain):
    # `remove_blocks` used to compute `latest + 1 - blocks_start_index`, which
    # wrapped in release builds when reverting below the window: the truncate
    # became a no-op, stale blocks survived, and the next `add_block` tripped a
    # length assertion that crossed pyo3 as an uncatchable `PanicException`.
    snapshot = chain.snapshot()
    transfer(chain, 4 * BLOCK_HISTORY)
    assert chain.blocks.first_number > 0, "expected pruning to have advanced the window"

    chain.revert(snapshot)
    transfer(chain, 10)
    assert chain.blocks["latest"] is not None
    assert replays(chain.txs[-1])


def test_block_only_workloads_are_pruned_too(chain):
    # `chain.mine()` loops and the account setters produce blocks with no
    # transaction in them, so the send path alone would never trim them.
    for _ in range(1000):
        chain.mine()
    assert chain.blocks.first_number > 0
    mined = chain.blocks.first_number

    account = chain.accounts[0]
    for i in range(1000):
        account.balance = 10**21 + i
    assert chain.blocks.first_number > mined
    assert chain.blocks["latest"] is not None
    with pytest.raises(HistoryPrunedError):
        chain.blocks[0]


@pytest.mark.parametrize("chain", [8], indirect=True)
def test_connect_picks_up_the_configured_window(chain):
    transfer(chain, 100)
    assert block_depth(chain) <= 8 + 1
    assert replays(chain.txs[-1])


@pytest.mark.parametrize("chain", [None], indirect=True)
def test_unlimited_history_keeps_everything_replayable(chain):
    transfer(chain, 2 * BLOCK_HISTORY + 100)

    assert chain.txs.first_index == 0
    assert replays(chain.txs[0])
    assert chain.txs[0].call_trace is not None


@pytest.mark.parametrize("chain", [None], indirect=True)
def test_unlimited_history_survives_snapshot_and_revert(chain):
    # With retention off there is nothing to re-seed and the snapshot must not
    # clone the whole transaction list to find that out.
    transfer(chain, 100)
    latest = chain.blocks["latest"].number
    total = len(chain.txs)

    snapshot = chain.snapshot()
    transfer(chain, 100)
    chain.revert(snapshot)

    assert chain.blocks["latest"].number == latest
    assert len(chain.txs) == total
    assert chain.txs.first_index == 0
    assert replays(chain.txs[0])
    assert chain.blocks[0].number == 0


@pytest.mark.skipif(platform.system() != "Linux", reason="needs /proc and malloc_trim")
def test_retention_flattens_instead_of_growing_linearly(chain):
    transfer(chain, 2000)  # warm up allocators
    base = rss()

    transfer(chain, 10000)
    first = rss() - base
    transfer(chain, 10000)
    second = rss() - base

    # Unpruned this cost ~2.1 KB/tx and rose linearly: the second half would add
    # as much as the first. Pruned, the resident set plateaus.
    assert second < first + 2 * 1024 * 1024, (first, second)
    assert second / 20000 < 200, second / 20000


@pytest.mark.skipif(platform.system() != "Linux", reason="needs /proc and malloc_trim")
@pytest.mark.parametrize("chain", [8], indirect=True)
def test_retention_plateaus_with_automine_off(chain):
    # The block count is the hard limit: fat blocks raise the plateau but do not
    # remove it. 8 blocks of 250 transactions plateaus at ~2000 retained, so a run
    # six times longer than the window must not grow.
    with chain.change_automine(False):
        for _ in range(2 * 8):  # fill the window twice over
            transfer(chain, 250)
            chain.mine()
        base = rss()
        for _ in range(6 * 8):
            transfer(chain, 250)
            chain.mine()
        grown = rss() - base

    assert grown < 8 * 1024 * 1024, grown
    assert tx_depth(chain) <= (8 + 1) * 250


@pytest.mark.parametrize("chain", [8], indirect=True)
def test_snapshots_preserve_the_blockhash_horizon(chain):
    # `BLOCKHASH` reaches 256 blocks back however little metadata the chain keeps,
    # and reverting makes the snapshot's block the tip again - sliding that horizon
    # back onto numbers the chain has long since passed. Deriving the retained
    # hashes from the block window conflated the two: with `block_history = 8` the
    # window is 9 blocks and the horizon is 257. It failed silently, because on a
    # local chain a missing hash is not a miss that refetches the right value, it
    # is one the underlying database synthesizes.
    target = chain.accounts[2]
    target.code = BLOCKHASH_CODE
    while chain.blocks["latest"].number < 300:
        chain.mine()
    height = chain.blocks["latest"].number
    probed = (height - 100).to_bytes(32, "big")
    baseline = target.call(data=probed)
    assert int.from_bytes(baseline, "big") != 0, "expected a real hash"

    snapshot = chain.snapshot()
    while chain.blocks["latest"].number < height + 500:
        chain.mine()  # advances the tip past the horizon, pruning the hash cache
    chain.revert(snapshot)

    assert chain.blocks["latest"].number == height
    # Same block, same height, and nothing had to be put back: the snapshot kept
    # its horizon from being pruned for as long as it was live.
    assert target.call(data=probed) == baseline
