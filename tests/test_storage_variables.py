"""`read_storage_variable` and `write_storage_variable` on revm.

The storage layout of an account comes from its pytypes resolver when one is
set, otherwise from the contract recognized by its code.
"""
import asyncio
import subprocess
import sys

from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.config import WakeConfig
from wake.development.pytypes_generator import TypeGenerator

SOURCE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Store {
    uint256 public value;
    mapping(address => uint256) public balances;

    function set(uint256 v) external { value = v; }
}
"""

# Runs in a subprocess so the generated `pytypes` package does not leak into
# other tests through `sys.modules`.
CHECK = """
from wake.testing import *

from pytypes.contracts.Store import Store


@chain.connect()
def check():
    store = Store.deploy()
    store.set(7)
    assert read_storage_variable(store, "value") == 7

    write_storage_variable(store, "value", 42)
    assert store.value() == 42

    owner = chain.accounts[0]
    write_storage_variable(store, "balances", 5, keys=[owner])
    assert store.balances(owner) == 5
    assert read_storage_variable(store, "balances", keys=[owner]) == 5

    account = Account(store.address)
    assert read_storage_variable(account, "value") == 42
    account.pytypes_resolver = Store
    assert read_storage_variable(account, "value") == 42


check()
"""


def test_storage_variables(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "Store.sol").write_text(SOURCE)

    config = WakeConfig(project_root_path=tmp_path)
    config.load_configs()
    compiler = SolidityCompiler(config)
    compiler.load()
    asyncio.run(
        compiler.compile(
            {contracts / "Store.sol"},
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
