"""Generated pytypes annotations must evaluate at runtime.

Pytypes use `from __future__ import annotations`, so importing them never
evaluates an annotation. A missing import or a wrong number of type arguments
goes unnoticed until something calls `get_type_hints`, which the JSON-RPC
`chain.txs` lookup does to decode a transaction's return value.
"""
import asyncio
import subprocess
import sys

import pytest

from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.config import WakeConfig
from wake.development.pytypes_generator import TypeGenerator

SOURCE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

// Generated in the same module as `Holder`, so unless renamed it would shadow
// the `Call` that the generated annotations refer to.
struct Call { address target; bytes callData; }

contract Holder {
    uint256 public stored;

    function set(uint256 value) external { stored = value; }
    function get() external view returns (uint256) { return stored; }
    function pair() external pure returns (uint256, bool) { return (1, true); }
    function target(Call calldata c) external pure returns (address) { return c.target; }
}
"""

# Runs in a subprocess so the generated `pytypes` package does not leak into
# other tests through `sys.modules`.
CHECK = """
from typing_extensions import get_args, get_origin, get_overloads, get_type_hints

from pytypes.contracts.Holder import Call_, Holder
from wake.development.call import Call
from wake.development.core import Address
from wake.development.primitive_types import uint256
from wake.development.transactions import _decoded_return_type


def call_types(function):
    hints = [get_type_hints(f)["return"] for f in (function, *get_overloads(function))]
    found = set()
    for hint in hints:
        for t in (hint, *get_args(hint)):
            if get_origin(t) is Call:
                assert len(get_args(t)) == 1, t
                found.add(get_args(t)[0])
    return found


# `Call.return_value` is the decoded result whatever the request type.
assert call_types(Holder.deploy) == {bytes}, call_types(Holder.deploy)
assert call_types(Holder.set) == {type(None)}, call_types(Holder.set)
assert call_types(Holder.get) == {uint256}, call_types(Holder.get)
assert call_types(Holder.stored) == {uint256}, call_types(Holder.stored)
# Overloads spell the tuple `Tuple[...]` and the implementation `tuple[...]`.
pair = {(get_origin(t), get_args(t)) for t in call_types(Holder.pair)}
assert pair == {(tuple, (uint256, bool))}, call_types(Holder.pair)

assert _decoded_return_type(Holder.set) is type(None)
assert _decoded_return_type(Holder.get) is uint256
assert _decoded_return_type(Holder.pair) == tuple[uint256, bool]
assert _decoded_return_type(Holder.target) is Address
"""


@pytest.mark.slow
def test_generated_annotations_evaluate(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "Holder.sol").write_text(SOURCE)

    config = WakeConfig(project_root_path=tmp_path)
    config.load_configs()
    compiler = SolidityCompiler(config)
    compiler.load()
    asyncio.run(
        compiler.compile(
            {contracts / "Holder.sol"},
            [SolcOutputSelectionEnum.ALL],
            write_artifacts=True,
            force_recompile=False,
            no_warnings=True,
        )
    )
    TypeGenerator(config, False).generate_types(compiler)

    result = subprocess.run(
        [sys.executable, "-c", CHECK], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
