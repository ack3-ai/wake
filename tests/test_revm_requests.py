"""Call requests through generated pytypes on revm.

A deploy call returns the runtime code without decoding it against the
constructor ABI.
"""
import asyncio
import subprocess
import sys

from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.config import WakeConfig
from wake.development.pytypes_generator import TypeGenerator

SOURCE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Emitter {
    uint256 public stored;

    event Stored(uint256 value);

    constructor(uint256 initial) { stored = initial; }

    function store(uint256 value) external {
        stored = value;
        emit Stored(value);
    }

    function senderBalance() external view returns (uint256) { return msg.sender.balance; }
}
"""

# Runs in a subprocess so the generated `pytypes` package does not leak into
# other tests through `sys.modules`.
CHECK = """
from wake.testing import *

from pytypes.contracts.Emitter import Emitter


@chain.connect()
def check():
    emitter = Emitter.deploy(1)

    assert Emitter.deploy(1, request_type="call") == emitter.code
    assert Emitter.deploy(1, request_type="call", return_call=True).return_value == emitter.code


check()
"""


def test_deploy_call_request(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "Emitter.sol").write_text(SOURCE)

    config = WakeConfig(project_root_path=tmp_path)
    config.load_configs()
    compiler = SolidityCompiler(config)
    compiler.load()
    asyncio.run(
        compiler.compile(
            {contracts / "Emitter.sol"},
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
