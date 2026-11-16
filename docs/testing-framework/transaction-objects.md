# Transaction objects

When sending a transaction, a transaction object is returned. This object can be used to access
the transaction's properties, such as its status, events or return value.
Transactions are executed immediately, so the object is final as soon as it is returned.

A `tx_callback` can be registered on a `Chain` instance. The callback receives a single argument,
the transaction object, and is called for every transaction. This can be used to process all transactions in a single place.

```python
from wake.testing import *
from pytypes.contracts.Counter import Counter


def tx_callback(tx: TransactionAbc):
    print(tx.console_logs)


@chain.connect()
def test_callback():
    chain.tx_callback = tx_callback

    counter = Counter.deploy()
    counter.increment()
```

## Transaction properties

Every transaction object has the following properties:

| Property                           | Description                                                                                                  |
|------------------------------------|--------------------------------------------------------------------------------------------------------------|
| `block`                            | `Block` the transaction was mined in (the pending block while automine is disabled and the block is not mined) |
| `block_number`                     | number of that block                                                                                         |
| `call_trace`                       | `CallTrace` instance representing the call trace of the transaction                                         |
| `chain`                            | `Chain` the transaction was sent to                                                                          |
| `console_logs`                     | list of `console.log` calls made by the transaction                                                          |
| <nobr>`cumulative_gas_used`</nobr> | gas consumed by this and all previous transactions in the same block                                         |
| `data`                             | data sent in the transaction                                                                                 |
| `error`                            | native (`pytypes`) revert error, `None` if the transaction succeeded                                         |
| `events`                           | tuple of native (`pytypes`) events emitted by the transaction                                                |
| `from_`                            | `Account` the transaction was sent from                                                                      |
| `gas_used`                         | gas used by the transaction                                                                                  |
| `raw_error`                        | `UnknownRevertError` instance, `None` if the transaction succeeded                                           |
| `raw_events`                       | list of `UnknownEvent` instances emitted by the transaction                                                  |
| `raw_return_value`                 | raw return value of the transaction; `Account` for contract deployment, `bytes` otherwise; raises `error` if the transaction failed |
| `return_value`                     | return value of the transaction; raises `error` if the transaction failed                                    |
| `status`                           | `ExecutionStatusEnum.SUCCESS` or `ExecutionStatusEnum.FAILURE`                                               |
| `to`                               | `Account` the transaction was sent to                                                                        |
| `tx_hash`                          | string hash of the transaction                                                                               |
| `tx_index`                         | index of the transaction in the block                                                                        |

`call_trace` and `console_logs` re-execute the transaction, so they are only available while its block is within the chain history, see [Chain history](./chains-and-blocks.md#chain-history).
The remaining properties are stored with the object and stay available for as long as it is referenced.

Properties of the signed transaction, such as `nonce`, `value`, `gas_limit`, `type` or the signature, are available only on the Anvil and Hardhat engines, see [Anvil and Hardhat](./anvil-and-hardhat.md#transaction-objects).

## Multiple transactions in a single block

It is possible to send multiple transactions in a way that they are mined in the same block. This
can be achieved in the following steps:

1. Disable `automine` on the `Chain` instance
2. Send any number of transactions
3. Re-enable `automine`
4. Call `.mine()` on the `Chain` instance

```python
from wake.testing import *
from pytypes.contracts.Counter import Counter


@chain.connect()
def test_multiple_txs():
    counter = Counter.deploy()

    # temporarily disable automine
    with chain.change_automine(False):
        tx1 = counter.increment()
        tx2 = counter.increment()
        tx3 = counter.increment()

    chain.mine()

    assert tx1.block == tx2.block == tx3.block
```

While automine is disabled, each transaction is executed right away in the pending block, so its status, events and return value are available before the block is mined.

!!! tip "Changing `automine`"
    While it is possible to change the `automine` property of a `Chain` instance manually, it is not recommended.
    If an exception is raised before `automine` is re-enabled, the chain is left with `automine` disabled.
    This can be overcome by using the `change_automine` context manager.
