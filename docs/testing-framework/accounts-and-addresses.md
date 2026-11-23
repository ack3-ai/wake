---
description: "Account and Address in Wake tests: importing accounts, signing messages, assigning labels, low-level calls and transactions, and contract accounts."
---

# Accounts and addresses

Wake testing framework strictly distinguishes between accounts and addresses.
However, in most cases, API functions accept both `Account` and `Address` types.

## Addresses

`Address` is a 20-byte value encoded as a hex string. It can be constructed from
a hex string or an integer:

```python
from wake.testing import Address

Address("0x0000000000000000000000000000000000000000")
Address(0)
```

The hex string does not have to be [EIP-55](https://eips.ethereum.org/EIPS/eip-55) compliant, but `str(address)` always returns the EIP-55 checksummed form.

Addresses can be compared with each other:

```python
from wake.testing import Address

assert Address(1) > Address(0)
```

An `Address` is equal only to another `Address`. Comparing it with a string returns `False`, and ordering it against a string raises `TypeError`, so construct an `Address` first:

```python
assert token.owner() == Address("0xdAC17F958D2ee523a2206206994597C13D831ec7")
```

## Accounts

`Account` is an `Address` bound to a specific `Chain`. It can be constructed from
an `Address`, a hex string or an integer. Optionally, a chain can be specified, otherwise
the `chain` global object is used:

```python
from wake.testing import Account, Chain, chain

other_chain = Chain()

assert Account(0) == Account(0, chain)
assert Account(0) != Account(0, other_chain)
```

An `Account` is never equal to an `Address`, and ordering an `Account` against an `Address` raises `TypeError`; compare `account.address` instead.
`Account` instances belonging to different chains cannot be compared using the `<` and `>` operators.

!!! warning "Using accounts belonging to different chains"
    An `Account` refers to its own chain. When an account of one chain is needed on another chain, for example as a function argument,
    pass its `address` property instead.

### Importing accounts and addresses

`Account` and `Address` instances can be imported from a private key:

```python
from wake.testing import Account, Address

Account.from_key("0x" + "a" * 64)
Address.from_key("0x" + "a" * 64)
```

From a mnemonic:

```python
from wake.testing import Account, Address

Account.from_mnemonic(" ".join(["test"] * 11 + ["junk"]))
Address.from_mnemonic(" ".join(["test"] * 11 + ["junk"]))
```

Or from an alias (see [Managing accounts with private keys](./deployment.md#managing-accounts-with-private-keys)):

```python
from wake.testing import Account, Address

Account.from_alias("alice")
Address.from_alias("alice")
```

It is also possible to create a new account with a random private key:

```python
from wake.testing import Account

Account.new()
```

In all of the above cases, a private key is stored together with the account and can be used to sign transactions or messages.

An account can also be backed by a Trezor hardware wallet, which signs instead of a stored private key:

```python
from wake.testing import Account

Account.from_trezor("m/44'/60'/0'/0/0")
```

### Signing messages

`Account` instances can be used to sign messages. This is only possible if the account has a known private key.
The private key must be imported using one of the methods described in the previous section or must be owned by
the client (the account must be present in `chain.accounts`).

#### Signing raw messages

Using `account.sign(message)` it is possible to sign any message in the form of bytes:

```python
from wake.testing import Account

account = Account.from_mnemonic(" ".join(["test"] * 11 + ["junk"]))
signature = account.sign(b"Hello, world!")
```

The message is signed according to the [EIP-191](https://eips.ethereum.org/EIPS/eip-191) standard (version `0x45`).

#### Signing structured messages

Using `account.sign_structured(message)` it is possible to sign structured messages:

```python
from wake.testing import *
from dataclasses import dataclass


@dataclass
class Transfer:
    sender: Address
    recipient: Address
    amount: uint256


@chain.connect()
def test_sign_structured():
    account = Account.from_mnemonic(" ".join(["test"] * 11 + ["junk"]))
    signature = account.sign_structured(
        Transfer(
            sender=account.address,
            recipient=Address(1),
            amount=10,
        ),
        domain=Eip712Domain(
            name="Test",
            chainId=chain.chain_id,
        )
    )
```

See [EIP-712](https://eips.ethereum.org/EIPS/eip-712) for more information.

To debug a signature, the hashes that are signed can be computed directly. `get_eip712_struct_hash(message)` returns the struct hash and `get_eip712_signing_hash(message, domain)` the full signing hash.
Structs generated in `pytypes` derive from `Struct` and also offer `.encode_eip712_type()`, `.encode_eip712_data()`, `.get_eip712_struct_hash()` and `.get_eip712_signing_hash(domain)`.

#### Signing EIP-7702 authorizations

`account.sign_authorization(address)` signs an [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702) authorization that delegates the account's code to the contract at `address`.
`chain_id` and `nonce` default to the current chain and the account's current nonce.
The signed authorization is passed to a transaction, sent by any account, in the `authorization_list` argument:

```python
from wake.testing import *
from pytypes.contracts.Counter import Counter


@chain.connect()
def test_delegation():
    implementation = Counter.deploy()
    eoa = Account.new()

    authorization = eoa.sign_authorization(implementation)
    chain.accounts[0].transact(authorization_list=[authorization])

    # eoa now executes the code of Counter
    Counter(eoa).increment()
    assert Counter(eoa).count() == 1
```

#### Signing message hash

While it is not recommended to sign message hashes directly, it is sometimes necessary.
To sign a message hash, use `account.sign_hash(message_hash)`:

```python
from wake.testing import *

account = Account.from_mnemonic(" ".join(["test"] * 11 + ["junk"]))
signature = account.sign_hash(keccak256(b"Hello, world!"))
```

!!! warning
    Always sign a message hash only if you know the original message.

### Assigning labels

`Account` instances can be assigned labels. Labels override the default string representation
of the account:

```python
from wake.testing import Account

account = Account(0)
account.label = "ZERO"
```

Setting the label to `None` removes the label.

### Account properties

`Account` instances have the following properties:

| Property                         | Description                                                                                      |
|----------------------------------|--------------------------------------------------------------------------------------------------|
| `address`                        | `Address` of the account                                                                         |
| `balance`                        | balance of the account in Wei                                                                    |
| `chain`                          | `Chain` the account is bound to                                                                  |
| `code`                           | code of the account                                                                              |
| `label`                          | string label of the account                                                                      |
| `nonce`                          | nonce of the account                                                                             |
| `private_key`                    | private key of the account (if known)                                                            |
| <nobr>`pytypes_resolver`</nobr>  | contract from `pytypes` used to decode events and errors of the account, see below (`None` by default) |

Except for `address`, `chain` and `private_key`, all properties can be assigned to.
Assigning `balance`, `nonce` or `code` mines a new block.

### Resolving events and errors

Events and errors are decoded using the contract that emitted or raised them. When Wake cannot tell which contract that is, for example for a contract deployed from raw creation code or on a forked chain, `pytypes_resolver` names it explicitly:

```python
usdt = Account("0xdAC17F958D2ee523a2206206994597C13D831ec7")
usdt.pytypes_resolver = IUSDT
```

where `IUSDT` is a contract or interface generated in `pytypes`. The resolver must be set on the account whose code emits the event or raises the error; behind a proxy, that is the implementation contract.
Resolvers are cleared when the chain connects again, and assigning `None` removes one.
On forked chains, events and errors of contracts without a resolver and outside `pytypes` are decoded as `ExternalEvent` and `ExternalError` when their ABI is available on Sourcify or Etherscan, see [Events and errors](./events-and-errors.md).

### Low-level calls and transactions

Each `Account` instance has `call`, `transact`, `estimate` and `access_list` methods that can be used to perform arbitrary
requests (see [Interacting with contracts](./interacting-with-contracts.md)):

```python
from wake.testing import *


@chain.connect()
def test_accounts():
    alice = chain.accounts[0]
    bob = chain.accounts[1]

    alice.balance = 100
    bob.balance = 0

    bob.transact(value=10, from_=alice)
    assert alice.balance == 90
    assert bob.balance == 10
```

The previous example shows how to transfer Wei from one account to another.

!!! info "Low-level contract creation"
    To deploy a contract from creation code, use `chain.deploy`:

    ```python
    chain.deploy(Counter.get_creation_code())
    ```

## Contract accounts

Contract accounts are accounts that have non-empty code. Everything that applies to `Account` instances
also applies to contract accounts. However, contract accounts have additional methods:

- `get_creation_code` - returns the code used to deploy the contract, may require addresses of libraries needed by the contract,
- `deploy` - deploys the contract, requires equivalent arguments as the constructor of the contract in Solidity,
- other contract-specific methods generated in `pytypes`, including getters for public state variables.

```python
from pytypes.contracts.Counter import Counter

assert len(Counter.get_creation_code()) > 0
print(Counter.setCount.selector.hex())
```

Every method of a contract generated in `pytypes` has a `selector` property.

!!! tip "Constructing contracts from an address"
    The ability to construct a contract from an address (and an optional `Chain` instance) can be very useful
    when interacting with contracts through proxies:

    ```python
    from wake.testing import *
    from pytypes.contracts.Counter import Counter
    from pytypes.openzeppelin.contracts.proxy.ERC1967.ERC1967Proxy import ERC1967Proxy

    @chain.connect()
    def test_proxy():
        impl = Counter.deploy()
        proxy = ERC1967Proxy.deploy(impl, b"")

        # behave as if Counter was deployed at proxy.address
        counter = Counter(proxy.address)
        counter.increment()
        assert counter.count() == 1
    ```

    The example project does not include `@openzeppelin/contracts`.
    To run this test, install it with `npm install @openzeppelin/contracts` and import `@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol` from a Solidity file in the project so that `pytypes` are generated for `ERC1967Proxy`.
