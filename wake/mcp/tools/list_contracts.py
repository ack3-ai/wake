from pathlib import Path
from typing import Literal

from pydantic import Field

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc


class ListContractsInput(ToolInput):
    paths: list[str] | None = Field(
        None, description="List of files and directories to search for contracts"
    )
    kind_filter: list[
        Literal["contract", "abstract contract", "library", "interface"]
    ] | None = Field(
        None,
        description="List of contract kinds to filter by (returns all if not provided)",
    )


@mcp_tool
def list_contracts(
    input: ListContractsInput, *, build: McpBuild, compilation_root: Path, **kwargs
) -> str:
    """List all contracts in the project."""
    raw_paths = input.paths or []
    kind_filter = input.kind_filter or [
        "contract",
        "abstract contract",
        "library",
        "interface",
    ]

    if not raw_paths:
        filter_paths = None
    else:
        filter_paths = []
        for p in raw_paths:
            try:
                filter_paths.append(Path(p).relative_to(Path.cwd()))
            except ValueError:
                filter_paths.append(Path(p))

    lines: list[str] = []
    for p, source_unit in build.source_units.items():
        try:
            p = p.relative_to(compilation_root)
        except ValueError:
            pass

        if filter_paths is None or any(p.is_relative_to(fp) for fp in filter_paths):
            for contract in source_unit.contracts:
                if contract.kind not in kind_filter:
                    continue
                if contract.abstract and "abstract contract" not in kind_filter:
                    continue
                kind = contract.kind.value
                if contract.abstract and kind == "contract":
                    kind = "abstract contract"
                lines.append(f"- {kind} {contract.name} @ {node_loc(contract, build)}")

    if not lines:
        return "No contracts found."
    return f"Contracts ({len(lines)}):\n" + "\n".join(lines)
