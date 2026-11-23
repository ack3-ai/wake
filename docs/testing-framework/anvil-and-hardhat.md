---
description: "Running Wake tests on an Anvil or Hardhat development chain over JSON-RPC instead of the default in-process revm engine, and what differs between them."
---

# Anvil and Hardhat

Tests run on the in-process `revm` engine by default.
Instead, they can run on an [Anvil](https://github.com/foundry-rs/foundry/tree/master/crates/anvil) or [Hardhat](https://github.com/NomicFoundation/hardhat) development chain that Wake talks to over JSON-RPC.
This is useful to connect to an already running node, to let external tools attach to the testing chain, or to test against the behavior of a specific client.
Hardhat support is deprecated.

This page lists what differs from the rest of the testing framework documentation, which describes the default engine.

## Switching the engine

The engine is selected in `wake.toml`:

```toml
[testing]
cmd = "anvil"  # or "hardhat"
```

The development chain has to be installed separately. Anvil is under active development, so keep it up to date:

```shell
foundryup
```

Command-line arguments passed to the launched chain are set by the `cmd_args` options of the [`testing.anvil` and `testing.hardhat` namespaces](../configuration.md#testinganvil-namespace).

## Connecting to a chain

`chain.connect()` launches a new development chain for the duration of the test.
Given a URI, it connects to an already running chain instead:

- an HTTP connection (e.g. `http://localhost:8545`),
- a WebSocket connection (e.g. `ws://localhost:8545`),
- an IPC socket (e.g. `/tmp/anvil.ipc`).

```python
from wake.testing import *


@chain.connect("ws://localhost:8545")
def test_counter():
    print(chain.chain_id)
```

When launching a new chain, the `accounts`, `chain_id`, `fork` and `hardfork` keyword arguments override the command-line arguments from the configuration, and two more keyword arguments are accepted:

| Keyword argument         | Description                             | Default value |
|--------------------------|-----------------------------------------|---------------|
| `min_gas_price`          | minimum gas price accepted by the chain | `0`           |
| `block_base_fee_per_gas` | base fee per gas for the next block     | `0`           |

None of the keyword arguments can be used when connecting to a running chain or when working with Hardhat.

## Chain members

Besides the members described in [Chains and blocks](./chains-and-blocks.md), JSON-RPC chains have the following properties:

| Property                         | Description                                                                                     |
|----------------------------------|-------------------------------------------------------------------------------------------------|
| `client_version`                 | client version as reported by the `web3_clientVersion` JSON-RPC method                          |
| <nobr>`default_tx_confirmations`</nobr> | default number of confirmations (mined blocks) needed before a transaction object is returned |
| `gas_price`                      | gas price used for transactions that do not specify one                                         |
| <nobr>`max_priority_fee_per_gas`</nobr> | max priority fee per gas used for transactions that do not specify one                   |
| `require_signed_txs`             | whether to send signed transactions or unsigned transactions                                    |

All of them except `client_version` can be assigned to. JSON-RPC chains also have the following methods:

| Method                                         | Description                                                             |
|------------------------------------------------|-------------------------------------------------------------------------|
| `mine_many`                                    | mine multiple blocks with an optional timestamp delta between blocks    |
| `reset`                                        | reset the chain to its initial state                                    |
| `set_min_gas_price`                            | set the minimum gas price accepted by the chain                         |
| <nobr>`set_next_block_base_fee_per_gas`</nobr> | set the base fee per gas for the next block                             |
| `update_accounts`                              | update the accounts list                                                |

`chain.txs` can also be indexed by a transaction hash (a string starting with `0x`), `chain.blocks` supports `len()` and iteration, and `chain.chain_interface` exposes the JSON-RPC methods of the node (`get_block`, `get_transaction`, `debug_trace_transaction`, `get_logs`, ...), with `type` being a property rather than a method.

The `testing.block_history` option does not apply: the development chain keeps its own history.

## Transaction arguments

JSON-RPC chains implement the following transaction arguments in addition to those described in [Interacting with contracts](./interacting-with-contracts.md#keyword-arguments):

| Argument                            | Description                                                                                       |
|-------------------------------------|---------------------------------------------------------------------------------------------------|
| `gas_limit="auto"`                  | estimate the gas limit before sending the transaction                                            |
| `access_list="auto"`                | create the access list before sending the transaction                                            |
| `max_fee_per_gas`                   | maximum fee per gas of an [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559) transaction         |
| <nobr>`max_priority_fee_per_gas`</nobr> | maximum priority fee per gas of an EIP-1559 transaction                                       |
| `confirmations`                     | number of blocks to be mined before the transaction object is returned; `0` returns it immediately after the transaction is sent |

The transaction type is derived from the arguments. `revert=False` (`revert_on_failure=False`) for `estimate` and `access_list` requests is not supported.

!!! warning "Sending transactions from any account"
    The `from_` argument can be used to send transactions from any account (including contract) or address by impersonating it.
    However, this may come at a cost of decreased performance (see [Performance considerations](performance-considerations.md)).

    **When sending transactions from an account with code (contract), the contract behaves as if it had no code during the execution of the transaction!**

## Transaction objects

With `confirmations=0`, a transaction object is returned before the transaction is mined.
`tx.wait()` waits for it, and accessing properties that need the receipt, e.g. `status`, `events`, `return_value` or `error`, waits implicitly.
A revert error of such a transaction is not raised; it is available in `tx.error`.
`tx.status` is `ExecutionStatusEnum.PENDING` until the transaction is mined, and `tx_callback` is not invoked for transactions sent with `confirmations=0`.

Transaction objects additionally have the following properties:

| Property              | Description                                              |
|-----------------------|----------------------------------------------------------|
| `chain_id`            | chain ID of the transaction                              |
| `effective_gas_price` | effective gas price of the transaction                   |
| `gas_limit`           | gas limit specified in the transaction                   |
| `nonce`               | nonce specified in the transaction                       |
| `r`, `s`              | parts of the ECDSA signature                             |
| `type`                | type of the transaction                                  |
| `value`               | amount of Wei sent in the transaction                    |

and depending on the transaction type:

| Transaction type                                                   | Additional properties                                                                 |
|--------------------------------------------------------------------|---------------------------------------------------------------------------------------|
| `LegacyTransaction` (type `0`)                                     | `gas_price`, `v`                                                                      |
| `Eip2930Transaction` (type `1`, [EIP-2930](https://eips.ethereum.org/EIPS/eip-2930)) | `access_list`, `gas_price`, `y_parity`                              |
| `Eip1559Transaction` (type `2`, [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559)) | `access_list`, `max_fee_per_gas`, `max_priority_fee_per_gas`, `y_parity` |
| `Eip7702Transaction` (type `4`, [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702)) | `access_list`, `authorization_list`, `max_fee_per_gas`, `max_priority_fee_per_gas`, `y_parity` |

To send multiple transactions in a single block, disable automine, send the transactions with `confirmations=0` and `gas_limit="auto"`, re-enable automine and call `chain.mine()`.

## Blocks

Block objects additionally have the following properties: `base_fee_per_gas`, `difficulty`, `extra_data`, `gas_limit`, `logs_bloom`, `miner`, `mix_hash`, `nonce`, `parent_hash`, `receipts_root`, `seal_fields`, `sha3_uncles`, `size`, `state_root`, `total_difficulty`, `transactions_root` and `uncles`.

## Other differences

- An account's `nonce` can only be incremented.
- The contract code size limit ([EIP-170](https://eips.ethereum.org/EIPS/eip-170)) and initcode size limit ([EIP-3860](https://eips.ethereum.org/EIPS/eip-3860)) are enforced, while the default engine does not enforce them, see [Troubleshooting](./troubleshooting.md).
