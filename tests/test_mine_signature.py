"""`Chain.mine` takes the same argument name on both engines.

The two implementations had the same semantics -- an optional callable that
receives the latest block timestamp and returns the new one -- under different
parameter names: `timestamp_change` on `wake.development.core.Chain` (and in
Wake 4), `callback` on the `revm` chain. Positional calls worked either way, but
`chain.mine(timestamp_change=...)` raised

    TypeError: Chain.mine() got an unexpected keyword argument 'timestamp_change'

on the default engine, so a suite could not use the keyword form portably.

The `revm` parameter is now named `timestamp_change` too.

Asserted findings:

* the Python implementation still names it `timestamp_change` - this is the side
  the name was unified *on*, so a rename here would silently re-open the gap;
* the keyword form works on the `revm` chain and actually moves the timestamp;
* the positional and no-argument forms are unaffected.
"""

import inspect

import pytest

from wake.development.core import Chain as PythonChain
from wake_rs import Chain as RevmChain


def test_python_implementation_still_names_it_timestamp_change():
    # Guards the invariant from the other direction: if this is ever renamed, the
    # revm signature has to follow, and this test is where that gets noticed.
    params = list(inspect.signature(PythonChain.mine).parameters)
    assert params[1:] == ["timestamp_change"]


def test_keyword_form_advances_the_timestamp():
    chain = RevmChain()
    with chain.connect():
        before = chain.blocks["latest"].timestamp

        chain.mine(timestamp_change=lambda t: t + 3600)

        assert chain.blocks["latest"].timestamp == before + 3600


def test_positional_form_still_works():
    chain = RevmChain()
    with chain.connect():
        before = chain.blocks["latest"].timestamp

        chain.mine(lambda t: t + 60)

        assert chain.blocks["latest"].timestamp == before + 60


def test_no_argument_form_still_mines():
    chain = RevmChain()
    with chain.connect():
        before = chain.blocks["latest"].number

        chain.mine()

        assert chain.blocks["latest"].number == before + 1
