"""Regression tests for incremental compilation correctness.

These drive ``SolidityCompiler`` through sequences of edits and assert that the
incremental build stays equivalent to a full from-scratch build of the same
final state — in particular that compiler warnings are never silently lost when
the compilation-unit structure changes without the warning's file being
recompiled.
"""
import asyncio
import random
import shutil
import tempfile
from pathlib import Path

import pytest

from wake.compiler import SolcOutputSelectionEnum, SolidityCompiler
from wake.compiler.compiler import CompilationFileSystemEventHandler
from wake.compiler.solc_frontend import SolcOutputErrorSeverityEnum
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


@pytest.mark.slow
def test_incremental_skipped_cu_orphan_not_leaked(project):
    """A carried-over file whose only compilation unit becomes un-compilable
    (no satisfiable solc version) must be dropped from the build *and* from
    source_units_info — it must not be left claiming to be a built source unit."""
    root, contracts, cfg = project
    compiler = SolidityCompiler(cfg)

    h8 = "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.0;\n"
    h6 = "// SPDX-License-Identifier: MIT\npragma solidity >=0.6.0 <0.7.0;\n"

    def files():
        return sorted(contracts.rglob("*.sol"))

    async def run():
        # S imports two independent leaves X and L; all ^0.8.0 -> one CU, compiles
        (contracts / "X.sol").write_text(
            h8 + "contract X { function fx() public pure returns (uint){ return 1; } }\n")
        (contracts / "L.sol").write_text(
            h8 + "contract L { function fl() public pure returns (uint){ return 2; } }\n")
        (contracts / "S.sol").write_text(
            h8 + 'import "./X.sol";\nimport "./L.sol";\n'
            "contract S { function s(X x, L l) public pure returns (uint){ return x.fx()+l.fl(); } }\n")
        await compiler.compile(files(), OUTPUT, write_artifacts=False)
        assert (contracts / "X.sol") in compiler.latest_build.source_units

        # change L to an incompatible version -> CU{S,X,L} can no longer compile.
        # X is unchanged and imports nothing that changed, so it is a carried-over
        # file orphaned only because its sole CU was skipped.
        (contracts / "L.sol").write_text(
            h6 + "contract L { function fl() public pure returns (uint){ return 2; } }\n")
        await compiler.compile(files(), OUTPUT, write_artifacts=False)

        built = {str(p.relative_to(root)) for p in compiler.latest_build.source_units}
        info_keys = set(compiler.latest_build_info.source_units_info)
        # source_units_info must never reference a unit that is not in the build
        assert info_keys <= built, f"leaked into source_units_info: {info_keys - built}"

        full = SolidityCompiler(cfg)
        await full.compile(files(), OUTPUT, write_artifacts=False)
        assert built == {str(p.relative_to(root)) for p in full.latest_build.source_units}
        assert info_keys == set(full.latest_build_info.source_units_info)

    asyncio.run(run())


@pytest.mark.slow
def test_fs_handler_config_reinclude_keeps_files(tmp_path):
    """When a config change re-includes previously-excluded files (found via the
    file-system event handler's rescan), a *subsequent unrelated* edit must not
    silently drop them — the handler must refresh its tracked file set."""
    root = tmp_path
    (root / "contracts").mkdir()
    (root / "vendor").mkdir()
    (root / "wake.toml").write_text('[compiler.solc]\nexclude_paths = ["vendor"]\n')
    (root / "contracts" / "A.sol").write_text(HEADER + "contract A { uint public x = 1; }\n")
    (root / "vendor" / "V.sol").write_text(HEADER + "contract V { uint public y = 2; }\n")

    cfg = WakeConfig(project_root_path=root)
    cfg.load_configs()
    compiler = SolidityCompiler(cfg)
    loop = asyncio.new_event_loop()
    try:
        included = {root / "contracts" / "A.sol"}
        loop.run_until_complete(
            compiler.compile(list(included), OUTPUT, write_artifacts=False)
        )
        handler = CompilationFileSystemEventHandler(
            cfg, set(included), loop, compiler, OUTPUT, write_artifacts=False
        )
        assert (root / "vendor" / "V.sol") not in compiler.latest_build.source_units

        # config change: stop excluding vendor/
        (root / "wake.toml").write_text("[compiler.solc]\nexclude_paths = []\n")
        handler._on_modified(cfg.local_config_path)
        loop.run_until_complete(handler._compile())
        assert (root / "vendor" / "V.sol") in compiler.latest_build.source_units

        # unrelated edit to A.sol — vendor/V.sol must survive
        (root / "contracts" / "A.sol").write_text(
            HEADER + "contract A { uint public x = 99; }\n"
        )
        handler._on_modified(root / "contracts" / "A.sol")
        loop.run_until_complete(handler._compile())
        assert (root / "vendor" / "V.sol") in compiler.latest_build.source_units, (
            "re-included vendor/V.sol was dropped after an unrelated edit"
        )
    finally:
        loop.close()
        shutil.rmtree(root, ignore_errors=True)


def _render_module(idx, value, imports, warn):
    s = HEADER
    imports = sorted(imports)
    for j in imports:
        s += f'import "./M{j}.sol";\n'
    body = [f"    uint public v = {value};",
            f"    function f{idx}() public pure returns (uint) {{ return {value}; }}"]
    for j in imports:
        body.append(
            f"    function use{j}(C{j} c) public pure returns (uint) {{ return c.f{j}(); }}"
        )
    if warn:
        body.append("    function w() public pure { uint unusedLocal; }")
    return s + f"contract C{idx} {{\n" + "\n".join(body) + "\n}\n"


@pytest.mark.slow
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_incremental_differential_fuzz(project, seed):
    """Random create/modify/delete/import edit sequences must keep the
    incremental build (source units, returned errors, and build info) equivalent
    to a full rebuild at every step."""
    root, contracts, cfg = project
    compiler = SolidityCompiler(cfg)
    rng = random.Random(seed)

    def files():
        return sorted(contracts.rglob("*.sol"))

    async def run():
        mods = {0: (rng.randint(0, 99), set(), False)}
        next_idx = 1
        present = set()

        for step in range(14):
            ids = list(mods)
            op = rng.choice(
                ["create", "modify", "delete", "add_import", "remove_import", "toggle_warn"]
            )
            if op == "create" or not ids:
                k = rng.randint(0, min(2, len(mods)))
                imps = set(rng.sample(list(mods), k)) if k else set()
                mods[next_idx] = (rng.randint(0, 99), imps, rng.random() < 0.3)
                next_idx += 1
            elif op == "delete":
                del mods[rng.choice(ids)]
            else:
                i = rng.choice(ids)
                val, imps, warn = mods[i]
                if op == "modify":
                    val = rng.randint(0, 99)
                elif op == "toggle_warn":
                    warn = not warn
                elif op == "add_import":
                    cand = [j for j in mods if j != i and j not in imps]
                    if cand:
                        imps = imps | {rng.choice(cand)}
                elif op == "remove_import" and imps:
                    imps = imps - {rng.choice(sorted(imps))}
                mods[i] = (val, imps, warn)

            # sync model to disk
            deleted = set()
            for i in list(present):
                if i not in mods:
                    (contracts / f"M{i}.sol").unlink()
                    deleted.add(contracts / f"M{i}.sol")
                    present.discard(i)
            for i, (val, imps, warn) in mods.items():
                (contracts / f"M{i}.sol").write_text(_render_module(i, val, imps, warn))
                present.add(i)

            _, inc_errors = await compiler.compile(
                files(), OUTPUT, write_artifacts=False, deleted_files=deleted
            )
            full = SolidityCompiler(cfg)
            _, full_errors = await full.compile(files(), OUTPUT, write_artifacts=False)

            ctx = f"seed={seed} step={step} op={op}"
            assert set(compiler.latest_build.source_units) == set(full.latest_build.source_units), \
                f"source units diverge ({ctx})"
            assert _err_fingerprints(inc_errors) == _err_fingerprints(full_errors), \
                f"errors/warnings diverge ({ctx})"
            assert set(compiler.latest_build_info.source_units_info) == \
                set(full.latest_build_info.source_units_info), f"source_units_info diverges ({ctx})"
            assert set(compiler.latest_build_info.compilation_units) == \
                set(full.latest_build_info.compilation_units), f"compilation_units diverge ({ctx})"

    asyncio.run(run())


def _solc_available(*versions):
    """Best-effort ensure the given solc versions are installed; skip if not."""
    from wake.core.solidity_version import SolidityVersion
    from wake.svm import SolcVersionManager

    svm = SolcVersionManager(WakeConfig(project_root_path=Path.home()))
    for vs in versions:
        v = SolidityVersion.fromstring(vs)
        if not svm.installed(v):
            try:
                asyncio.run(svm.install(v))
            except Exception:
                return False
    return True


@pytest.mark.slow
@pytest.mark.skipif(
    not _solc_available("0.8.28", "0.7.6"), reason="solc 0.8.28/0.7.6 unavailable"
)
def test_incremental_subproject_orphan_cascade_consistent(tmp_path):
    """Under subprojects, a non-subproject dependency whose only default-subproject
    compilation unit is version-skipped has no canonical SourceUnit; a full build
    cascade-drops every file depending on it. An incremental rebuild triggered by
    editing a file that merely shares a CU with such a dependent (without importing
    the dependency, and without being part of the skipped CU) must drop exactly the
    same files -- it must not keep a source unit whose dependency is missing, which
    previously diverged from a full build and crashed reference resolution."""
    root = tmp_path
    for d in ("v7", "v8", "lib", "def"):
        (root / d).mkdir()
    (root / "wake.toml").write_text(
        '[subproject."v7"]\npaths = ["v7"]\ntarget_version = "0.7.6"\n'
        '[subproject."v8"]\npaths = ["v8"]\ntarget_version = "0.8.28"\n'
    )
    h7 = "// SPDX-License-Identifier: MIT\npragma solidity 0.7.6;\n"
    h8 = "// SPDX-License-Identifier: MIT\npragma solidity 0.8.28;\n"
    hw = "// SPDX-License-Identifier: MIT\npragma solidity >=0.7.0;\n"
    (root / "lib" / "Dep.sol").write_text(
        hw + "library Dep { function v() internal pure returns (uint){ return 1; } }\n"
    )
    (root / "v8" / "V.sol").write_text(
        h8 + 'import "../lib/Dep.sol";\n'
        "contract V { function f() public pure returns (uint){ return Dep.v(); } }\n"
    )
    # T shares the v8 compilation unit with V but does not import V's dependency Dep
    (root / "v8" / "T.sol").write_text(
        h8 + 'import "./V.sol";\n'
        "contract T { function g(V v) public view returns (uint){ return v.f(); } }\n"
    )
    (root / "v7" / "Seven.sol").write_text(
        h7 + "contract Seven { function s() public pure returns (uint){ return 7; } }\n"
    )
    # default (no subproject) file mixing v7 and v8 -> version-skipped CU -> Dep
    # never gets a canonical SourceUnit
    (root / "def" / "Mixer.sol").write_text(
        hw + 'import "../lib/Dep.sol";\nimport "../v7/Seven.sol";\nimport "../v8/V.sol";\n'
        "contract Mixer { function m() public pure returns (uint){ return Dep.v(); } }\n"
    )

    cfg = WakeConfig(project_root_path=root)
    cfg.load_configs()
    compiler = SolidityCompiler(cfg)

    def files():
        return sorted(root.rglob("*.sol"))

    async def run():
        await compiler.compile(files(), OUTPUT, write_artifacts=False)
        # edit T (shares a CU with V, does not import Dep, not in the skipped CU)
        (root / "v8" / "T.sol").write_text(
            (root / "v8" / "T.sol").read_text() + "\n// touch\n"
        )
        _, inc_errors = await compiler.compile(files(), OUTPUT, write_artifacts=False)

        full = SolidityCompiler(cfg)
        _, full_errors = await full.compile(files(), OUTPUT, write_artifacts=False)

        assert set(compiler.latest_build.source_units) == set(
            full.latest_build.source_units
        ), "build source units diverge from a full build"
        assert set(compiler.latest_build_info.source_units_info) == set(
            full.latest_build_info.source_units_info
        ), "source_units_info diverges from a full build"
        assert _err_fingerprints(inc_errors) == _err_fingerprints(full_errors)

    asyncio.run(run())


@pytest.mark.slow
@pytest.mark.skipif(
    not _solc_available("0.8.28", "0.7.6"), reason="solc 0.8.28/0.7.6 unavailable"
)
def test_incremental_subproject_unorphan_readded(tmp_path):
    """A file orphaned because a dependency had no canonical SourceUnit (its only
    default-subproject CU was version-skipped) must be re-added by an incremental
    build once an *unrelated* edit gives that dependency a canonical CU again --
    even though the file itself is unchanged, so its own CU is not recompiled."""
    root = tmp_path
    for d in ("v7", "v8", "lib", "def"):
        (root / d).mkdir()
    (root / "wake.toml").write_text(
        '[subproject."v8"]\npaths = ["v8"]\ntarget_version = "0.8.28"\n'
        '[subproject."v7"]\npaths = ["v7"]\ntarget_version = "0.7.6"\n'
    )
    h7 = "// SPDX-License-Identifier: MIT\npragma solidity 0.7.6;\n"
    h8 = "// SPDX-License-Identifier: MIT\npragma solidity 0.8.28;\n"
    hw = "// SPDX-License-Identifier: MIT\npragma solidity >=0.7.0;\n"
    (root / "lib" / "Dep.sol").write_text(
        hw + "library Dep { function v() internal pure returns (uint){ return 1; } }\n"
    )
    (root / "v8" / "V.sol").write_text(
        h8 + 'import "../lib/Dep.sol";\n'
        "contract V { function f() public pure returns (uint){ return Dep.v(); } }\n"
    )
    (root / "v7" / "Seven.sol").write_text(h7 + "contract Seven { uint public s = 7; }\n")
    (root / "v8" / "Eight.sol").write_text(h8 + "contract Eight { uint public e = 8; }\n")
    # default file mixing v7 and v8 -> version-skipped CU; it is the only default CU
    # holding Dep, so Dep has no canonical SourceUnit and V (which imports Dep) is
    # orphaned
    (root / "def" / "Bridge.sol").write_text(
        hw + 'import "../lib/Dep.sol";\nimport "../v7/Seven.sol";\nimport "../v8/Eight.sol";\n'
        "contract Bridge { function b() public pure returns (uint){ return Dep.v(); } }\n"
    )

    cfg = WakeConfig(project_root_path=root)
    cfg.load_configs()
    compiler = SolidityCompiler(cfg)

    def files():
        return sorted(root.rglob("*.sol"))

    async def run():
        await compiler.compile(files(), OUTPUT, write_artifacts=False)
        # V is orphaned: its dependency Dep has no canonical SourceUnit
        assert (root / "v8" / "V.sol") not in compiler.latest_build.source_units

        # add a clean default importer of Dep -> Dep gets a canonical CU -> V is
        # buildable again. V is unchanged, so its own CU is not recompiled.
        (root / "def" / "Good.sol").write_text(
            hw + 'import "../lib/Dep.sol";\n'
            "contract Good { function g() public pure returns (uint){ return Dep.v(); } }\n"
        )
        _, inc_errors = await compiler.compile(files(), OUTPUT, write_artifacts=False)

        full = SolidityCompiler(cfg)
        _, full_errors = await full.compile(files(), OUTPUT, write_artifacts=False)

        assert (root / "v8" / "V.sol") in full.latest_build.source_units
        assert set(compiler.latest_build.source_units) == set(
            full.latest_build.source_units
        ), "incremental build did not re-add a file that a full build builds"
        assert set(compiler.latest_build_info.source_units_info) == set(
            full.latest_build_info.source_units_info
        )
        assert _err_fingerprints(inc_errors) == _err_fingerprints(full_errors)

    asyncio.run(run())
