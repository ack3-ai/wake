"""Regression tests for incremental compilation correctness.

These drive ``SolidityCompiler`` through sequences of edits and assert that the
incremental build stays equivalent to a full from-scratch build of the same
final state — in particular that compiler warnings are never silently lost when
the compilation-unit structure changes without the warning's file being
recompiled.
"""
import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.config import WakeConfig

OUTPUT = [SolcOutputSelectionEnum.ALL]
HEADER = "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.0;\n"

# A.sol contains an "Unused local variable" warning (solc code 2072).
A_WITH_WARNING = HEADER + "contract A { function f() public pure { uint unusedLocal; } }\n"
B_IMPORTS_A = HEADER + 'import "./A.sol";\ncontract Bc is A {}\n'


def _err_fingerprints(errors):
    out = set()
    for e in errors:
        loc = None
        if e.source_location is not None:
            loc = (e.source_location.file, e.source_location.start, e.source_location.end)
        out.add((str(e.error_code), e.severity.value, (e.message or "").strip(), loc))
    return out


@pytest.fixture()
def project(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    cfg = WakeConfig(project_root_path=tmp_path)
    cfg.load_configs()
    yield tmp_path, contracts, cfg
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.slow
def test_incremental_warning_preserved_across_cu_rematerialization(project):
    """A warning in A.sol must survive A's compilation unit being absorbed into
    a larger CU and then re-materialising on its own — even though A itself is
    never edited and therefore never recompiled."""
    root, contracts, cfg = project
    compiler = SolidityCompiler(cfg)

    async def run():
        # 1) A alone -> CU{A}; warning present
        (contracts / "A.sol").write_text(A_WITH_WARNING)
        _, e1 = await compiler.compile([contracts / "A.sol"], OUTPUT, write_artifacts=False)
        assert any(str(e.error_code) == "2072" for e in e1), "warning missing at step 1"

        # 2) add B importing A -> A absorbed into CU{A,B}; warning still present
        (contracts / "B.sol").write_text(B_IMPORTS_A)
        _, e2 = await compiler.compile(
            [contracts / "A.sol", contracts / "B.sol"], OUTPUT, write_artifacts=False
        )
        assert any(str(e.error_code) == "2072" for e in e2), "warning missing at step 2"

        # 3) delete B -> CU{A} re-materialises; A is unchanged so it is not
        #    recompiled. The warning must NOT be lost.
        (contracts / "B.sol").unlink()
        _, e3 = await compiler.compile(
            [contracts / "A.sol"], OUTPUT, write_artifacts=False,
            deleted_files={contracts / "B.sol"},
        )
        assert any(str(e.error_code) == "2072" for e in e3), (
            "incremental build lost the unused-variable warning after the CU "
            "re-materialised"
        )

        # ground truth: a full build of the final state reports the same errors
        full = SolidityCompiler(cfg)
        _, ef = await full.compile([contracts / "A.sol"], OUTPUT, write_artifacts=False)
        assert _err_fingerprints(e3) == _err_fingerprints(ef)

    asyncio.run(run())


@pytest.mark.slow
def test_incremental_build_info_matches_full(project):
    """After an add/delete churn that reshapes the import graph, the incremental
    build_info's set of compilation units must match a full build's."""
    root, contracts, cfg = project
    compiler = SolidityCompiler(cfg)

    def files():
        return sorted(contracts.rglob("*.sol"))

    async def run():
        (contracts / "A.sol").write_text(
            HEADER + "contract A { function f() public pure returns (uint){ return 1; } }\n"
        )
        await compiler.compile(files(), OUTPUT, write_artifacts=False)

        (contracts / "B.sol").write_text(
            HEADER + 'import "./A.sol";\ncontract Bc is A {}\n'
        )
        await compiler.compile(files(), OUTPUT, write_artifacts=False)

        (contracts / "B.sol").unlink()
        await compiler.compile(
            files(), OUTPUT, write_artifacts=False, deleted_files={contracts / "B.sol"}
        )

        full = SolidityCompiler(cfg)
        await full.compile(files(), OUTPUT, write_artifacts=False)

        inc_cus = set(compiler.latest_build_info.compilation_units)
        full_cus = set(full.latest_build_info.compilation_units)
        assert inc_cus == full_cus
        assert set(compiler.latest_build.source_units) == set(full.latest_build.source_units)

    asyncio.run(run())
