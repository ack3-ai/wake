"""Hardfork names passed to `Chain.connect` are normalized.

revm's own `SpecId::FromStr` accepts exactly one spelling per fork, so
`hardfork="cancun"` raised `ValueError: Invalid hardfork` while `"Cancun"` worked,
and the three forks revm spells differently from common usage (`Spurious`,
`Tangerine`, `Merge`) rejected the names most people reach for
(`SpuriousDragon`, `TangerineWhistle`, `Paris`).

`parse_hardfork` in wake_rs/src/chain.rs now lowercases, drops `-`/`_` separators
and maps those aliases before handing the value to revm.

Asserted findings:

* every canonical revm name is still accepted;
* capitalization and separators no longer matter;
* `Paris`, `SpuriousDragon` and `TangerineWhistle` resolve to the forks they name -
  and resolve *semantically*, not merely without raising, which is checked with
  PUSH0 (introduced in Shanghai, so an invalid opcode on any earlier fork);
* an unknown name still raises, and the message lists what is accepted.
"""

import pytest

from wake_rs import Chain

CANONICAL = [
    "Frontier",
    "Homestead",
    "Tangerine",
    "Spurious",
    "Byzantium",
    "Petersburg",
    "Istanbul",
    "Berlin",
    "London",
    "Merge",
    "Shanghai",
    "Cancun",
    "Prague",
    "Osaka",
    "Amsterdam",
]

# PUSH0 then STOP. PUSH0 arrived in Shanghai, so this is an invalid opcode before it.
PUSH0_STOP = bytes.fromhex("5f00")


def connect_with(hardfork):
    chain = Chain()
    return chain, chain.connect(hardfork=hardfork)


@pytest.mark.parametrize("hardfork", CANONICAL)
def test_canonical_names_accepted(hardfork):
    chain, ctx = connect_with(hardfork)
    with ctx:
        assert chain.connected


@pytest.mark.parametrize(
    "hardfork",
    ["cancun", "CANCUN", "cAnCuN", "shanghai", "london", "berlin", "oSaKa"],
)
def test_capitalization_is_ignored(hardfork):
    chain, ctx = connect_with(hardfork)
    with ctx:
        assert chain.connected


@pytest.mark.parametrize(
    "hardfork",
    ["SpuriousDragon", "spurious_dragon", "TangerineWhistle", "tangerine-whistle"],
)
def test_separators_and_long_fork_names_accepted(hardfork):
    chain, ctx = connect_with(hardfork)
    with ctx:
        assert chain.connected


def push0_accepted(hardfork: str) -> bool:
    """Whether PUSH0 executes, i.e. whether the resolved fork is Shanghai or later."""
    chain = Chain()
    with chain.connect(hardfork=hardfork):
        target = chain.accounts[1]
        target.code = PUSH0_STOP
        try:
            target.call(data=b"")
            return True
        except Exception:
            return False


@pytest.mark.parametrize("alias,canonical", [("paris", "Merge"), ("Paris", "Merge")])
def test_alias_resolves_to_the_same_fork(alias, canonical):
    # Not just "does not raise": the alias must select the same fork semantics.
    assert push0_accepted(alias) == push0_accepted(canonical) is False


def test_pre_shanghai_and_post_shanghai_differ():
    # Guards the check above: if PUSH0 were accepted everywhere the test would be vacuous.
    assert push0_accepted("Shanghai") is True
    assert push0_accepted("London") is False


@pytest.mark.parametrize("hardfork", ["Latest", "Constantinople", "Dencun", "", "nonsense"])
def test_unknown_names_raise_with_the_accepted_list(hardfork):
    chain = Chain()
    with pytest.raises(ValueError) as excinfo:
        with chain.connect(hardfork=hardfork):
            pass

    message = str(excinfo.value)
    assert "Invalid hardfork" in message
    # The message must name what to use instead.
    assert "Cancun" in message and "Osaka" in message
