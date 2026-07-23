from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc, resolve_contract


class AnalyzeStateVariablesInput(ToolInput):
    contract_name: str = Field(..., description="Name of the contract to analyze")
    file_path: str | None = Field(
        None, description="Path to the Solidity file where the contract is defined"
    )


@mcp_tool
def analyze_state_variables(
    input: AnalyzeStateVariablesInput, *, build: McpBuild, **kwargs
) -> str:
    """Analyze state variables in a specific Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)

    if (
        contract.compilation_info is None
        or contract.compilation_info.storage_layout is None
    ):
        raise ValueError(
            f"Storage layout is not available for contract {input.contract_name}"
        )

    lines: list[str] = []

    for state_variable in contract.compilation_info.storage_layout.storage:
        var = build.reference_resolver.resolve_node(
            state_variable.ast_id,
            contract.source_unit.cu_hash,
        )
        assert isinstance(var, ir.VariableDeclaration)
        where = f"slot {state_variable.slot} offset {state_variable.offset}"
        lines.append(
            f"- {var.name}: {var.type_string} — {var.visibility} {var.mutability}, "
            f"{where} @ {node_loc(var, build)}"
        )

    # also list immutables (not part of the storage layout)
    for base_contract in contract.linearized_base_contracts:
        for var in base_contract.declared_variables:
            if var.mutability == ir.enums.Mutability.IMMUTABLE:
                lines.append(
                    f"- {var.name}: {var.type_string} — {var.visibility} "
                    f"{var.mutability}, no storage slot @ {node_loc(var, build)}"
                )

    if not lines:
        return f"No state variables in {input.contract_name}."
    return f"State variables of {input.contract_name} ({len(lines)}):\n" + "\n".join(
        lines
    )
