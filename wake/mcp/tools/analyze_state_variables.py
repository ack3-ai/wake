from typing import TypedDict

from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import Location, ToolInput, mcp_tool, node_to_location, resolve_contract


class AnalyzeStateVariablesInput(ToolInput):
    contract_name: str = Field(..., description="Name of the contract to analyze")
    file_path: str | None = Field(
        None, description="Path to the Solidity file where the contract is defined"
    )


class StateVariable(TypedDict):
    name: str
    type: str
    visibility: str
    mutability: str
    storage_slot: int | None
    storage_slot_offset: int | None
    location: Location


@mcp_tool
def analyze_state_variables(
    input: AnalyzeStateVariablesInput, *, build: McpBuild, **kwargs
) -> list[StateVariable]:
    """Analyze state variables in a specific Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)

    if (
        contract.compilation_info is None
        or contract.compilation_info.storage_layout is None
    ):
        raise ValueError(
            f"Storage layout is not available for contract {input.contract_name}"
        )

    storage_layout = contract.compilation_info.storage_layout

    result = []

    for state_variable in storage_layout.storage:
        var = build.reference_resolver.resolve_node(
            state_variable.ast_id,
            contract.source_unit.cu_hash,
        )
        assert isinstance(var, ir.VariableDeclaration)
        result.append(
            StateVariable(
                name=var.name,
                type=var.type_string,
                visibility=var.visibility,
                mutability=var.mutability,
                storage_slot=state_variable.slot,
                storage_slot_offset=state_variable.offset,
                location=node_to_location(var, build),
            )
        )

    # also list immutables
    for base_contract in contract.linearized_base_contracts:
        for var in base_contract.declared_variables:
            if var.mutability == ir.enums.Mutability.IMMUTABLE:
                result.append(
                    StateVariable(
                        name=var.name,
                        type=var.type_string,
                        visibility=var.visibility,
                        mutability=var.mutability,
                        storage_slot=None,
                        storage_slot_offset=None,
                        location=node_to_location(var, build),
                    )
                )

    return result
