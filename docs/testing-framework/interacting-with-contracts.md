# Interacting with contracts

Contracts can be interacted with either using methods generated in `pytypes` or using low-level methods.

## Request types

There are 4 low-level methods that represent different request types:

- `tx` - a request that sends a transaction (even if the function being called does not modify the blockchain state),
- `call` - a request that returns the return value of the function being called, does not modify the blockchain state (even if the function being called modifies the blockchain state),
- `estimate` - a request that returns an estimated amount of gas needed to perform the transaction,
- `access_list` - a request that returns an access list (addresses and storage keys, see [EIP-2930](https://eips.ethereum.org/EIPS/eip-2930)) and an estimated amount of gas needed to perform the transaction when the access list is used.

The low-level methods are named `.transact()`, `.call()`, `.estimate()`, and `.access_list()` respectively.
Each request type has its default account used when no `from_` argument is provided. The default accounts are properties of the `Chain` object:

- `chain.default_tx_account` for `tx` request type,
- `chain.default_call_account` for `call` request type,
- `chain.default_estimate_account` for `estimate` request type,
- `chain.default_access_list_account` for `access_list` request type.

All default accounts use `chain.accounts[0]` as the default value. The default accounts can be changed by assigning a new value to the corresponding property or by using the `set_default_accounts()` method.

```python
from wake.testing import *


@chain.connect()
def test_accounts():
    # assign each default account manually
    chain.default_tx_account = chain.accounts[1]
    chain.default_call_account = chain.accounts[1]
    chain.default_estimate_account = chain.accounts[1]
    chain.default_access_list_account = chain.accounts[1]

    # or assign all default accounts at once
    chain.set_default_accounts(chain.accounts[2])
```

!!! note
    It is recommended to set `default_estimate_account` and `default_access_list_account` to the same account as `default_tx_account` to ensure that the returned gas estimate is accurate.

In `pytypes`, the default request type is `tx` for non-pure non-view functions and `call` for pure and view functions.

```python
from wake.testing import *
from pytypes.contracts.Counter import Counter


@chain.connect()
def test_accounts():
    chain.set_default_accounts(chain.accounts[0])
    counter = Counter.deploy()

    # performs a call
    count = counter.count()

    # sends a transaction
    tx = counter.increment()
```

The request type can be changed using the `request_type` flag.

```python
# does not increment the counter
ret_val = counter.increment(request_type="call")

# "tx" request type is the default for non-pure non-view functions
tx = counter.increment(request_type="tx")

# amount of gas needed to send as a transaction
gas_estimate = counter.increment(request_type="estimate")

# access list and amount of gas needed to send as a transaction
access_list, gas_estimate = counter.increment(request_type="access_list")
```

The `call` request type used on the `.deploy()` method returns runtime code of the contract that would be deployed if the method was called with `tx` request type.

```python
# does not deploy the contract
runtime_code = Counter.deploy(request_type="call")

# deploys the contract and returns the contract instance, the default behavior
counter = Counter.deploy(request_type="tx")

# deploys the contract and returns the transaction object
tx = Counter.deploy(request_type="tx", return_tx=True)

# amount of gas needed to deploy the contract
gas_estimate = Counter.deploy(request_type="estimate")

# access list and amount of gas needed to deploy the contract
access_list, gas_estimate = Counter.deploy(request_type="access_list")
```

## Keyword arguments

Both methods generated in `pytypes` and low-level methods accept the following keyword arguments common for all request types:

| Argument                            | Description                                                                                                                                                         |
|-------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `from_`                             | `Account` or `Address` used as a sender of a transaction/call. If not provided, the default account for the request type will be used.                              |
| `value`                             | Amount of Ether to be sent. Can be either an `int` in Wei or a string with a unit (e.g. `"1 ether"`).                                                               |
| `gas_limit`                         | Maximum amount of gas that can be consumed by the transaction. Either an `int` or `"max"`.                                                                          |
| `gas_price`                         | Gas price. Can be either an `int` in Wei or a string with a unit (e.g. `"10 gwei"`).                                                                                |
| `access_list`                       | Access list to be used, a mapping of accounts to lists of storage slots. See [EIP-2930](https://eips.ethereum.org/EIPS/eip-2930) for more information.              |
| <nobr>`authorization_list`</nobr>   | List of [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702) authorizations signed with [`Account.sign_authorization`](./accounts-and-addresses.md#signing-eip-7702-authorizations). |

`max_fee_per_gas`, `max_priority_fee_per_gas`, `gas_limit="auto"` and `access_list="auto"` are accepted too, but only the Anvil and Hardhat engines implement them; the default engine raises `NotImplementedError`.
Use an explicit value instead, e.g. a gas limit from an `estimate` request or an access list from an `access_list` request.
See [Anvil and Hardhat](./anvil-and-hardhat.md#transaction-arguments) for how they behave there.

Low-level methods also accept the `data` keyword argument (of type `bytes`) that can be used to specify the data to be sent to a contract.

!!! tip "Encoding data for low-level calls and transactions"
    To prepare the `data` payload, the `Abi` helper class can be used. It offers the same ABI encoding
    functions as the `abi` global object in Solidity.

    ```python
    from wake.testing import *
    from pytypes.contracts.Counter import Counter

    @chain.connect()
    def test_low_level_transact():
        chain.default_tx_account = chain.accounts[0]

        counter = Counter.deploy()

        # execute counter.setCount(100) using a low-level transaction
        counter.transact(data=Abi.encode_call(Counter.setCount, [100]))
        assert counter.count() == 100
    ```

Methods generated in `pytypes` accept the `to` keyword argument (of type `Account`, `Address` or hex-encoded string address) that can be used to override the address of the contract being called.

!!! tip "Calling contracts through a proxy"
    Using the `to` keyword argument can be useful when a contract should be called through a proxy contract.

    ```python
    contract.initialize(owner, to=proxy)
    ```

## `tx` request type

The `tx` request type is used to send a transaction. Transactions are executed immediately, so the returned value (or the transaction object with `return_tx=True`) is already final.
The `confirmations` keyword argument is accepted for compatibility and has no effect on the default engine.

The `from_` argument can be used to send transactions from any account or address, including a contract.

## `call` request type

The `call` request type is used to execute a call. It accepts one more keyword argument, `block`, that can be used to specify the number of the block to be used as a context for the call.
The default value is `latest` which means that the call will be executed in the context of the latest block.

## `estimate` request type

The `estimate` request type is used to estimate the amount of gas needed to execute a transaction. It accepts one more keyword argument, `block`, that can be used to specify the number of the block to be used as a context for the estimation.
The default value is `pending` which means that the estimation will be executed in the context of the pending block.

## `access_list` request type

The `access_list` request type is used to estimate the access list and the amount of gas needed to execute a transaction when using the returned access list.
It accepts one more keyword argument, `block`, that can be used to specify the number of the block to be used as a context for the estimation.
The default value is `pending` which means that the estimation will be executed in the context of the pending block.

!!! info "Requests in older blocks"
    Chains keep only the newest `testing.block_history` blocks (256 by default), so the `block` argument can only name a block within that window.
    Naming an older block raises `HistoryPrunedError`, see [Chains and blocks](./chains-and-blocks.md#chain-history).

## Estimating reverting transactions

By default, `estimate` and `access_list` requests raise the revert error when the transaction would revert.
Setting `revert=False` returns the estimate or the access list anyway (the low-level methods call the same argument `revert_on_failure`).

```python
# the counter is 0, so decrementing reverts
gas_estimate = counter.decrement(request_type="estimate", revert=False)
access_list, gas_estimate = counter.decrement(request_type="access_list", revert=False)
```

`revert` can be set to `False` only for `estimate` and `access_list` requests, and only on the default engine.

## Inspecting a request with `Call`

`call`, `estimate` and `access_list` requests return a `Call` object instead of the plain result when `return_call=True` is passed.
A `Call` describes the request together with its result, so it can be inspected from several angles without repeating it:

- `chain`, `block`, `data`, `from_`, `to`, `status`,
- `return_value` and `raw_return_value`; for `deploy`, the raw value is the runtime code,
- `error` and `raw_error`,
- `call_trace`,
- `estimated_gas` and `access_list`, regardless of the request type used.

Values the request did not already produce are computed on first access.

```python
call = counter.increment(request_type="call", return_call=True)
print(call.estimated_gas)
print(call.access_list)
print(call.call_trace)
```

`return_call` cannot be combined with the `tx` request type; use `return_tx=True` to get the transaction object instead.
Combined with `revert=False`, the `Call` of a reverting request has its `error` set while `estimated_gas` and `access_list` remain available.
