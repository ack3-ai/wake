---
description: "The Chain object in Wake tests: chain properties and methods, connecting to a chain, accessing blocks and their properties, and chain history."
---

# Chains and blocks

For single chain tests, Wake provides the global `chain` variable. This
variable is a `Chain` object that can be used to change the chain parameters
or access the chain data. Other `Chain` instances can be created, which is
useful in [Cross-chain testing](cross-chain-testing.md).

This page describes chains of the default `revm` engine. Chains of the Anvil and Hardhat engines offer additional members, see [Anvil and Hardhat](./anvil-and-hardhat.md).

## Chain properties

The `Chain` object has the following properties:

| Property                                   | Description                                                                                                |
|--------------------------------------------|------------------------------------------------------------------------------------------------------------|
| `accounts`                                 | list of `Account` objects with known private keys; each starts with 10000 ETH                              |
| `automine`                                 | whether to automatically mine a block for each transaction                                                 |
| `blocks`                                   | property to access the chain blocks                                                                        |
| `block_gas_limit`                          | gas limit of the pending block                                                                             |
| `chain_id`                                 | chain ID                                                                                                   |
| `chain_interface`                          | low-level chain interface with `type()`, `get_code`, `get_storage_at` and `set_storage_at`                 |
| `coinbase`                                 | coinbase `Account`, i.e. account of the miner who mined the block                                          |
| `connected`                                | whether the chain is connected                                                                             |
| <nobr>`default_access_list_account`</nobr> | default `Account` used for access list creation requests                                                   |
| <nobr>`default_call_account`</nobr>        | default `Account` used for calls                                                                           |
| <nobr>`default_estimate_account`</nobr>    | default `Account` used for gas estimations                                                                 |
| <nobr>`default_tx_account`</nobr>          | default `Account` used for transactions                                                                    |
| `forked_chain_id`                          | chain ID of the forked chain, or `None` when not forking                                                   |
| `txs`                                      | transaction objects of the chain, indexed by their position (`chain.txs[-1]` is the latest transaction)    |
| `tx_callback`                              | callback function called with every transaction object once the transaction is executed                    |

`automine`, `block_gas_limit`, `coinbase`, the four default accounts and `tx_callback` can be assigned to.

## Chain methods

The `Chain` object has the following methods:

| Method                                         | Description                                                                                |
|------------------------------------------------|--------------------------------------------------------------------------------------------|
| `change_automine`                              | context manager to temporarily change the `automine` property                              |
| `connect`                                      | context manager to create a chain for the duration of the context                          |
| `deploy`                                       | low-level method for deploying contracts from creation code                                |
| `mine`                                         | mine a block with an optional callback function to set the next block timestamp            |
| `revert`                                       | revert the chain to a previous state given by a snapshot ID                                |
| `set_default_accounts`                         | set the default accounts for `tx`, `call`, `estimate`, and `access_list` requests at once  |
| <nobr>`set_next_block_timestamp`</nobr>        | set the timestamp of the next block                                                        |
| `snapshot`                                     | take a snapshot of the chain state; return a snapshot ID                                   |
| <nobr>`snapshot_and_revert`</nobr>             | context manager to take a snapshot and revert to it after the context ends                 |

It is recommended to use the context managers `change_automine` and `snapshot_and_revert` instead of setting the `automine` property directly or calling `snapshot` and `revert` manually.

The following example presents the use of `Chain` methods:

```python
from wake.testing import chain


def test_chain():
    # create a chain and connect to it
    with chain.connect(), chain.snapshot_and_revert():
        # mine a block with the timestamp 1 greater than the previous block
        chain.mine(lambda x: x + 1)
```

`connect` and `change_automine` can also be used as decorators:

```python
from wake.testing import chain


@chain.connect()
@chain.change_automine(False)
def test_chain():
    # mine a block with the timestamp 1 greater than the previous block
    chain.mine(lambda x: x + 1)
```

### Connecting to a chain

The `connect` context manager accepts the following keyword-only arguments:

| Keyword argument | Description                                                                              | Default value            |
|------------------|------------------------------------------------------------------------------------------|--------------------------|
| `accounts`       | number of accounts to generate                                                           | `10`                     |
| `chain_id`       | chain ID assigned to the chain                                                           | `31337`, or the forked chain ID |
| `fork`           | URL of the chain to fork from, optionally followed by `@` and a block number             | `None` (no forking)      |
| `hardfork`       | hardfork to use, e.g. `"prague"`; the name is case-insensitive                           | `"osaka"`                |

```python
from wake.testing import chain


@chain.connect(
    accounts=15,
    chain_id=1020,
)
def test_chain():
    assert len(chain.accounts) == 15
    assert chain.chain_id == 1020
```

## Accessing chain blocks

The `chain.blocks` property can be used to access up-to-date chain blocks data.
It can be indexed by an integer, where negative numbers count back from the latest block, or by the string literals `latest`, `pending`, `earliest`, `safe`, and `finalized`:

```python
from wake.testing import chain
from pytypes.contracts.Counter import Counter


@chain.connect()
def test_chain_blocks():
    chain.set_default_accounts(chain.accounts[0])

    # get the block 0
    block0 = chain.blocks[0]
    # block 0 and earliest are the same
    assert block0.number == chain.blocks["earliest"].number

    counter = Counter.deploy()

    # find the first block with non-zero transactions count
    latest = chain.blocks["latest"].number
    block = next(
        chain.blocks[number]
        for number in range(chain.blocks.first_number, latest + 1)
        if len(chain.blocks[number].txs) > 0
    )

    assert block.txs[0].return_value == counter

    with chain.change_automine(False):
        # block -1 and latest are the same
        assert chain.blocks[-1].number == chain.blocks["latest"].number

        tx = counter.increment()

        # pending block contains the transaction
        assert tx in chain.blocks["pending"].txs
```

`chain.blocks` supports indexing only; it has no length and cannot be iterated directly, so loop over a range of block numbers as in the example.

## Block properties

Block objects have the following properties:

| Property      | Description                                                          |
|---------------|----------------------------------------------------------------------|
| `chain`       | chain the block belongs to                                           |
| `hash`        | block hash                                                           |
| `number`      | block number                                                         |
| `gas_used`    | amount of gas used in the block                                      |
| `timestamp`   | block timestamp                                                      |
| `txs`         | list of transaction objects in the block in execution order          |

The block gas limit and the miner are properties of the chain, `chain.block_gas_limit` and `chain.coinbase`.
Transactions of blocks before the fork block are not available through `txs` and raise `NotImplementedError`.

## Chain history

Each chain keeps only the newest `testing.block_history` blocks (256 by default, see [configuration](../configuration.md#testing-namespace)) together with the transactions in them, which keeps the memory use of long test runs bounded.
Reaching further back, e.g. `chain.blocks[0]`, `chain.txs[0]` or a request with an old `block` argument, raises `HistoryPrunedError`:

```python
from wake_rs import HistoryPrunedError
```

`chain.blocks.first_number` is the oldest retained block, and `chain.txs.first_index` the oldest retained transaction, so ranges such as `range(chain.txs.first_index, len(chain.txs))` stay within the window.

A transaction object keeps its metadata (`from_`, `to`, `status`, `gas_used`, `events`, `return_value`, ...) for as long as it is referenced, so keep the objects that are needed later instead of looking them up in `chain.txs`.
`call_trace` and `console_logs` re-execute the transaction and raise `HistoryPrunedError` once its block has left the window.

Disconnecting a chain discards its history; transaction objects keep their stored results, but re-executing them and reading `block.txs` raise `HistoryPrunedError`.
