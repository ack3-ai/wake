import asyncio

import pytest

import wake.ir as ir
import wake.ir.types as types
from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.compiler.solc_frontend import SolcOutputErrorSeverityEnum
from wake.config import WakeConfig
from wake.core.solidity_version import SolidityVersion, SolidityVersionExpr
from wake_detectors.require_custom_error_named_arguments_bug import (
    RequireCustomErrorNamedArgumentsBugDetector,
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
pragma solidity ^0.8.26;
library Errors {
    error Pair(uint256 a, uint256 b);
}
contract Base {
    error Inherited(uint256 a, uint256 b);
}
contract C is Base {
    error Pair(uint256 a, uint256 b);
    error Triple(uint256 a, uint256 b, uint256 c);
    error Single(uint256 a);
    error Empty();
    error Strings(string a, string b);
    error Mixed(string a, uint256 b, bytes c, bool d);
    error Dynamic(string a, uint256 b);
    event Event(uint256 a, uint256 b);
    struct Struct { uint256 a; uint256 b; }

    function reordered() external pure { require(false, Pair({b: 2, a: 1})); }
    function rotated() external pure { require(false, Triple({c: 3, a: 1, b: 2})); }
    function ordered() external pure { require(false, Pair({a: 1, b: 2})); }
    function positional() external pure { require(false, Pair(1, 2)); }
    function single() external pure { require(false, Single({a: 1})); }
    function empty() external pure { require(false, Empty()); }
    function conditionOnly() external pure { require(false); }
    function message() external pure { require(false, "message"); }
    function parenthesized() external pure { require(false, (((Pair({b: 2, a: 1}))))); }
    function parenthesizedCallee() external pure { require(false, (((Pair)))({b: 2, a: 1})); }
    function qualified() external pure { require(false, Errors.Pair({b: 2, a: 1})); }
    function inherited() external pure { require(false, Inherited({b: 2, a: 1})); }
    function standalone() external pure { revert Pair({b: 2, a: 1}); }
    function stringLiterals() external pure { require(false, Strings({b: "b", a: "a"})); }
    function parenthesizedLiterals() external pure { require(false, Strings({b: (("b")), a: (("a"))})); }
    function movedLiteral() external pure { require(false, Dynamic({b: 2, a: "a"})); }
    function mixedLiterals() external pure { require(false, Mixed({d: true, c: hex"cafe", b: 1, a: "a"})); }
    function convertedLiterals() external pure { require(false, Strings({b: string("b"), a: string("a")})); }
    function calldataString(string calldata a) external pure { require(false, Dynamic({b: 2, a: a})); }
    function orderedCalldataString(string calldata a) external pure { require(false, Dynamic({a: a, b: 2})); }
    function memoryString(string memory a) external pure { require(false, Dynamic({b: 2, a: a})); }
    function otherCall() external pure { helper({b: 2, a: 1}); }
    function structConstructor() external pure { Struct({b: 2, a: 1}); }
    function eventEmission() external { emit Event({b: 2, a: 1}); }
    function helper(uint256 a, uint256 b) internal pure { }
}
contract Shadow {
    function require(uint256 a, uint256 b) internal pure { }
    function shadowed() external pure { require({b: 2, a: 1}); }
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
    assert not any(
        e.severity == SolcOutputErrorSeverityEnum.ERROR for e in errors
    ), errors
    yield build.source_units[source]


@pytest.fixture
def detector():
    instance = RequireCustomErrorNamedArgumentsBugDetector()
    instance.extra = {"package_versions": {"eth-wake": "5.0.0rc3"}}
    return instance


def visit_identifiers(source_unit, detector, function_name):
    function = next(
        f
        for contract in source_unit.contracts
        for f in contract.functions
        if f.name == function_name
    )
    for node in function:
        if isinstance(node, ir.Identifier):
            detector.visit_identifier(node)


@pytest.mark.slow
@pytest.mark.parametrize(
    "function_name, expected",
    [
        ("reordered", 1),
        ("rotated", 1),
        ("ordered", 0),
        ("positional", 0),
        ("single", 0),
        ("empty", 0),
        ("conditionOnly", 0),
        ("message", 0),
        ("parenthesized", 1),
        ("parenthesizedCallee", 1),
        ("qualified", 1),
        ("inherited", 1),
        ("standalone", 0),
        ("stringLiterals", 0),
        ("parenthesizedLiterals", 0),
        ("movedLiteral", 0),
        ("mixedLiterals", 1),
        ("convertedLiterals", 1),
        ("calldataString", 1),
        ("orderedCalldataString", 0),
        ("memoryString", 1),
        ("otherCall", 0),
        ("structConstructor", 0),
        ("eventEmission", 0),
        ("shadowed", 0),
    ],
)
def test_expression_filtering(source_unit, detector, function_name, expected):
    visit_identifiers(source_unit, detector, function_name)
    results = detector.detect()
    assert len(results) == expected
    if expected:
        assert isinstance(results[0].detection.ir_node, ir.FunctionCall)
        assert isinstance(results[0].detection.ir_node.type, types.Error)


@pytest.mark.slow
@pytest.mark.parametrize(
    "pragma, expected",
    [
        ("^0.8.0", 1),
        ("0.8.25", 0),
        ("<0.8.26", 0),
        ("0.8.26", 1),
        ("0.8.36", 1),
        (">=0.8.26 <0.8.37", 1),
        (">=0.8.36", 1),
        ("0.8.25 || 0.8.36 || >=0.8.37", 1),
        ("<0.8.26 || >=0.8.37", 0),
        ("0.8.37", 0),
        (">=0.8.37", 0),
    ],
)
def test_version_filtering(source_unit, detector, monkeypatch, pragma, expected):
    monkeypatch.setattr(
        source_unit, "_version_ranges", SolidityVersionExpr(pragma).version_ranges
    )
    visit_identifiers(source_unit, detector, "reordered")
    assert len(detector.detect()) == expected
