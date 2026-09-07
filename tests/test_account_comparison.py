"""`Account` equality and inequality are complements.

`Account.__richcmp__` handled `Eq` and `Ne` with one expression: it forwarded the
operator to the address comparison and then ANDed the chain-identity check onto the
result. That is only correct for `Eq`. For `Ne` it computed

    address_ne and same_chain

instead of the negation of equality, so two accounts on different chains compared
unequal-False *and* equal-False at the same time -- violating the invariant that
`(a == b)` and `(a != b)` are complements, and breaking the documented

    assert Account(0) != Account(0, other_chain)

Asserted findings:

* `==` and `!=` are complements in every combination of address and chain;
* accounts sharing an address on different chains are unequal;
* `__hash__` still agrees with `==`, so dict and set behaviour is sound;
* ordering comparisons across chains still raise, and within a chain still work.
"""

import pytest

from wake_rs import Chain


@pytest.fixture(name="chains")
def chains_fixture():
    first, second = Chain(), Chain()
    with first.connect(), second.connect():
        yield first, second


def test_equality_and_inequality_are_complements(chains):
    first, second = chains
    account = first.accounts[0]
    cases = [
        first.accounts[0],  # same address, same chain
        first.accounts[1],  # different address, same chain
        second.accounts[0],  # same address, different chain
        second.accounts[1],  # different address, different chain
    ]

    for other in cases:
        assert (account == other) != (account != other)


def test_same_address_on_a_different_chain_is_not_equal(chains):
    first, second = chains
    account, twin = first.accounts[0], second.accounts[0]
    # The addresses genuinely match - both chains use the same test mnemonic.
    assert account.address == twin.address

    assert account != twin
    assert not (account == twin)


def test_same_account_is_equal_to_itself(chains):
    first, _ = chains
    assert first.accounts[0] == first.accounts[0]
    assert not (first.accounts[0] != first.accounts[0])


def test_hash_agrees_with_equality(chains):
    first, second = chains
    account, alias, twin = first.accounts[0], first.accounts[0], second.accounts[0]

    assert account == alias and hash(account) == hash(alias)
    # Unequal objects may collide, but these must not, or set/dict lookups conflate
    # accounts that compare unequal.
    assert hash(account) != hash(twin)
    assert len({account, alias, twin, first.accounts[1]}) == 3


def test_ordering_across_chains_still_raises(chains):
    first, second = chains
    with pytest.raises(ValueError, match="different chains"):
        first.accounts[0] < second.accounts[0]


def test_ordering_within_a_chain_still_works(chains):
    first, _ = chains
    a, b = first.accounts[0], first.accounts[1]
    assert (a < b) != (b < a)
