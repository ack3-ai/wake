from typing import Literal

from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc, resolve_contract


class ListContractFunctionsInput(ToolInput):
    contract_name: str = Field(
        ..., description="Name of the contract to list functions from"
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )
    mutability_filter: list[
        Literal["pure", "view", "nonpayable", "payable"]
    ] | None = Field(
        None,
        description="List of mutability types to filter by (returns all if not provided)",
    )
    visibility_filter: list[
        Literal["private", "internal", "public", "external"]
    ] | None = Field(
        None,
        description="List of visibility types to filter by (returns all if not provided)",
    )


@mcp_tool
def list_contract_functions(
    input: ListContractFunctionsInput, *, build: McpBuild, **kwargs
) -> str:
    """List all functions in a specific Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)

    mutability_filter = input.mutability_filter or [
        "pure",
        "view",
        "nonpayable",
        "payable",
    ]
    visibility_filter = input.visibility_filter or [
        "private",
        "internal",
        "public",
        "external",
    ]

    # functions not to be listed since they are overridden
    base_functions: set[ir.FunctionDefinition] = set()

    lines: list[str] = []

    for base_contract in contract.linearized_base_contracts:
        for function in base_contract.functions:
            base_functions.update(function.base_functions)

            # skip overridden and not implemented functions
            if function in base_functions or not function.implemented:
                continue

            if (
                function.state_mutability not in mutability_filter
                or function.visibility not in visibility_filter
            ):
                continue

            modifiers = [m.source for m in function.modifiers]
            mods = f", modifiers: {', '.join(modifiers)}" if modifiers else ""
            lines.append(
                f"- {function.canonical_name} — {function.visibility.value} "
                f"{function.state_mutability.value}{mods} @ {node_loc(function, build)}"
            )

    if not lines:
        return f"No functions in {input.contract_name}."
    return f"Functions of {input.contract_name} ({len(lines)}):\n" + "\n".join(lines)
