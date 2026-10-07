import asyncio

import pytest

import wake.ir as ir
from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.compiler.solc_frontend import SolcOutputErrorSeverityEnum
from wake.config import WakeConfig
from wake.core.solidity_version import SolidityVersion, SolidityVersionExpr
from wake_detectors.memory_byte_array_element_delete_bug import (
    MemoryByteArrayElementDeleteBugDetector,
)


@pytest.fixture
def source_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        WakeConfig,
        "max_solidity_version",
        property(lambda self: SolidityVersion.fromstring("0.8.37")),
    )
    source = tmp_path / "C.sol"
    source.write_text(
        """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;
contract C {
    bytes stored;

    function direct(bytes memory value) external pure { delete value[0]; }
    function parenthesized(bytes memory value) external pure { delete (((value[0]))); }
    function returned(bytes memory value) external pure { delete identity(value)[0]; }
    function converted(string memory value) external pure { delete bytes(value)[0]; }
    function nested(bytes[] memory value) external pure { delete value[0][0]; }
    function storageBytes() external { delete stored[0]; }
    function ordinaryArray(bytes1[] memory value) external pure { delete value[0]; }
    function wholeArray(bytes memory value) external pure { delete value; }
    function scalar(bytes1 value) external pure { delete value; }
    function increment(uint256 value) external pure { ++value; }

    function identity(bytes memory value) internal pure returns (bytes memory) {
        return value;
    }
}
"""
    )
    config = WakeConfig.fromdict(
        {"compiler": {"solc": {"target_version": "0.8.37"}}},
        project_root_path=tmp_path,
    )
    build, errors = asyncio.run(
        SolidityCompiler(config).compile(
            [source], [SolcOutputSelectionEnum.AST], write_artifacts=False
        )
    )
    assert not any(e.severity == SolcOutputErrorSeverityEnum.ERROR for e in errors)
    return build.source_units[source]


@pytest.fixture
def detector():
    instance = MemoryByteArrayElementDeleteBugDetector()
    instance.extra = {"package_versions": {"eth-wake": "5.0.0rc3"}}
    return instance


@pytest.mark.slow
@pytest.mark.parametrize(
    "function_name, expected",
    [
        ("direct", 1),
        ("parenthesized", 1),
        ("returned", 1),
        ("converted", 1),
        ("nested", 1),
        ("storageBytes", 0),
        ("ordinaryArray", 0),
        ("wholeArray", 0),
        ("scalar", 0),
        ("increment", 0),
    ],
)
def test_expression_filtering(source_unit, detector, function_name, expected):
    function = next(
        f for f in source_unit.contracts[0].functions if f.name == function_name
    )
    operations = [n for n in function if isinstance(n, ir.UnaryOperation)]
    assert len(operations) == 1
    detector.visit_unary_operation(operations[0])
    results = detector.detect()
    assert len(results) == expected
    if expected:
        assert results[0].detection.ir_node is operations[0]


@pytest.mark.slow
@pytest.mark.parametrize(
    "pragma, expected",
    [
        ("^0.8.0", 1),
        ("0.8.36", 1),
        (">=0.8.36", 1),
        ("0.8.36 || >=0.8.37", 1),
        ("0.8.37", 0),
        (">=0.8.37", 0),
        ("^0.8.37 || ^0.9.0", 0),
    ],
)
def test_version_filtering(source_unit, detector, monkeypatch, pragma, expected):
    monkeypatch.setattr(
        source_unit, "_version_ranges", SolidityVersionExpr(pragma).version_ranges
    )
    for node in source_unit.contracts[0].functions[0]:
        if isinstance(node, ir.UnaryOperation):
            detector.visit_unary_operation(node)
    assert len(detector.detect()) == expected
