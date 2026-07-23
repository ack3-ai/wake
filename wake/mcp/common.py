from dataclasses import dataclass
from pathlib import Path

from intervaltree import IntervalTree

from wake.ir import SourceUnit
from wake.ir.reference_resolver import ReferenceResolver


@dataclass
class McpBuild:
    project_root: Path
    relative_compilation_root: Path | None
    source_units: dict[Path, SourceUnit]
    interval_trees: dict[Path, IntervalTree]
    reference_resolver: ReferenceResolver
