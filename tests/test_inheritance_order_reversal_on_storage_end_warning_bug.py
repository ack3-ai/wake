import asyncio

import pytest

from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.compiler.solc_frontend import (
    SolcOutputErrorSecondarySourceLocation,
    SolcOutputErrorSeverityEnum,
)
from wake.config import WakeConfig
from wake.core.solidity_version import SolidityVersion, SolidityVersionExpr
from wake.detectors import DetectorConfidence, DetectorImpact
from wake_detectors.inheritance_order_reversal_on_storage_end_warning_bug import (
    InheritanceOrderReversalOnStorageEndWarningBugDetector,
)

pytestmark = pytest.mark.slow


@pytest.fixture
def compiled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        WakeConfig,
        "max_solidity_version",
        property(lambda self: SolidityVersion.fromstring("0.8.37")),
    )
    base = tmp_path / "Base.sol"
    base.write_text(
        """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;
contract Base { uint256 value; }
"""
    )
    source = tmp_path / "C.sol"
    source.write_text(
        """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;
import "./Base.sol";
contract Trigger is Base layout at 2**256 - 2**40 {}
contract Other is Base layout at 2**256 - 2**41 {}
contract Safe is Base layout at 100 {}
contract NoLayout is Base {}
contract NoInheritance layout at 2**256 - 2**40 { uint256 value; }
"""
    )
    config = WakeConfig.fromdict(
        {"compiler": {"solc": {"target_version": "0.8.37"}}},
        project_root_path=tmp_path,
    )
    compiler = SolidityCompiler(config)
    build, errors = asyncio.run(
        compiler.compile(
            [source, base], [SolcOutputSelectionEnum.AST], write_artifacts=False
        )
    )
    assert not any(
        e.severity == SolcOutputErrorSeverityEnum.ERROR for e in errors
    ), errors
    return build.source_units[source], compiler.latest_build_info


@pytest.fixture
def detector(compiled):
    instance = InheritanceOrderReversalOnStorageEndWarningBugDetector()
    instance.extra = {"package_versions": {"eth-wake": "5.0.0rc3"}}
    instance.build_info = compiled[1]
    return instance


@pytest.fixture
def contract(compiled):
    return next(c for c in compiled[0].contracts if c.name == "Trigger")


@pytest.fixture
def warning(contract, detector):
    cu_info = detector.build_info.compilation_units[contract.source_unit.cu_hash.hex()]
    return next(
        e
        for e in cu_info.errors
        if e.error_code == "3495"
        and e.source_location is not None
        and (e.source_location.start, e.source_location.end)
        == contract.storage_layout.byte_location
    )


@pytest.mark.parametrize(
    "contract_name, expected",
    [
        ("Trigger", 1),
        ("Other", 1),
        ("Safe", 0),
        ("NoLayout", 0),
        ("NoInheritance", 0),
    ],
)
def test_contract_filtering(compiled, detector, contract_name, expected):
    contract = next(c for c in compiled[0].contracts if c.name == contract_name)
    detector.visit_contract_definition(contract)
    results = detector.detect()
    assert len(results) == expected
    if expected:
        assert results[0].detection.ir_node is contract.storage_layout
        assert results[0].impact == DetectorImpact.MEDIUM
        assert results[0].confidence == DetectorConfidence.MEDIUM


@pytest.mark.parametrize(
    "pragma, expected",
    [
        ("^0.8.0", 1),
        ("0.8.28", 0),
        ("<0.8.29", 0),
        ("0.8.29", 1),
        ("0.8.35", 1),
        (">=0.8.29 <0.8.36", 1),
        (">=0.8.35", 1),
        ("0.8.28 || 0.8.35 || >=0.8.36", 1),
        ("<0.8.29 || >=0.8.36", 0),
        ("0.8.36", 0),
        (">=0.8.36", 0),
    ],
)
def test_version_filtering(contract, detector, monkeypatch, pragma, expected):
    monkeypatch.setattr(
        contract.source_unit,
        "_version_ranges",
        SolidityVersionExpr(pragma).version_ranges,
    )
    detector.visit_contract_definition(contract)
    assert len(detector.detect()) == expected


@pytest.mark.parametrize(
    "case, expected",
    [
        ("missing", 0),
        ("other-code", 0),
        ("missing-location", 0),
        ("wrong-file", 0),
        ("wrong-range", 0),
        ("other-contract", 0),
        ("secondary-only", 0),
        ("other-cu", 0),
        ("alias", 1),
        ("duplicate", 1),
    ],
)
def test_diagnostic_filtering(contract, detector, warning, case, expected):
    errors = [warning]
    cu_hash = contract.source_unit.cu_hash.hex()
    if case == "missing":
        errors = []
    elif case == "other-code":
        errors = [warning.model_copy(update={"error_code": "1234"})]
    elif case == "missing-location":
        errors = [warning.model_copy(update={"source_location": None})]
    elif case in {
        "wrong-file",
        "wrong-range",
        "other-contract",
        "secondary-only",
        "alias",
    }:
        location = warning.source_location
        if case == "wrong-file":
            location = location.model_copy(update={"file": "Base.sol"})
        elif case == "wrong-range":
            location = location.model_copy(update={"start": location.start + 1})
        elif case in {"other-contract", "secondary-only"}:
            other = next(c for c in contract.source_unit.contracts if c.name == "Other")
            start, end = other.storage_layout.byte_location
            location = location.model_copy(update={"start": start, "end": end})
        else:
            contract.source_unit._source_unit_names.add("alias.sol")
            location = location.model_copy(update={"file": "alias.sol"})
        update = {"source_location": location}
        if case == "secondary-only":
            update["secondary_source_locations"] = [
                SolcOutputErrorSecondarySourceLocation(
                    file=warning.source_location.file,
                    start=warning.source_location.start,
                    end=warning.source_location.end,
                    message="Layout here",
                )
            ]
        errors = [warning.model_copy(update=update)]
    elif case == "duplicate":
        errors = [warning, warning]

    cu_info = detector.build_info.compilation_units[cu_hash].model_copy(
        update={"errors": errors}
    )
    if case == "other-cu":
        cu_hash = "00" * 32
    detector.build_info = detector.build_info.model_copy(
        update={"compilation_units": {cu_hash: cu_info}}
    )
    detector.visit_contract_definition(contract)
    assert len(detector.detect()) == expected
