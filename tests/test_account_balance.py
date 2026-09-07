"""Development accounts must be pre-funded on connect, matching Anvil.

Anvil pre-funds each of its development accounts with 10000 ETH. The `revm` engine
derived its accounts from the same test mnemonic but assigned no balance, so a suite
that worked on Anvil failed on `revm` as soon as it moved value:

    RuntimeError: transaction validation error: lack of funds (0) for max fee (...)

`revm` is the default engine since 5.0, so that difference silently broke suites on
upgrade. `Chain._connect` now writes the balance straight to the database for both
the empty and the forked backend.

Asserted findings:

* every account in `chain.accounts` holds exactly 10000 ETH after connect;
* a value transfer succeeds with no manual funding;
* funding does not mine a block - `Account.balance`'s setter mines one per
  assignment, so funding through it would have cost a block per account and shifted
  every block number a suite asserts on;
* an explicit assignment still overrides the pre-funded balance.
"""

import pytest

from wake_rs import Chain

# Anvil's default, in wei: 10000 ETH == 0x21e19e0c9bab2400000.
ANVIL_DEFAULT_BALANCE = 10_000 * 10**18


@pytest.fixture(name="chain")
def chain_fixture():
    chain = Chain()
    with chain.connect():
        yield chain


def test_accounts_are_prefunded(chain):
    assert len(chain.accounts) == 10
    for account in chain.accounts:
        assert int(account.balance) == ANVIL_DEFAULT_BALANCE


def test_value_transfer_needs_no_manual_funding(chain):
    sender, recipient = chain.accounts[0], chain.accounts[1]
    before = int(recipient.balance)

    recipient.transact(data=b"", value=10**18, from_=sender)

    assert int(recipient.balance) == before + 10**18


def test_funding_does_not_mine_blocks(chain):
    # One block is mined by connect itself; pre-funding ten accounts must not add
    # ten more on top of it.
    assert chain.blocks["latest"].number == 0


def test_explicit_assignment_overrides_prefunded_balance(chain):
    account = chain.accounts[0]

    account.balance = 5 * 10**18

    assert int(account.balance) == 5 * 10**18
