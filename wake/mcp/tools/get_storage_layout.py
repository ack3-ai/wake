from typing import TypedDict

from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import Location, ToolInput, mcp_tool, node_to_location, resolve_contract


class GetStorageLayoutInput(ToolInput):
    contract_name: str = Field(
        ..., description="Name of the contract to get storage layout for"
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )


class StorageSlot(TypedDict):
    slot: int
    offset: int
    location: Location
    name: str
    type: str
    contract_name: str


@mcp_tool
def get_storage_layout(
    input: GetStorageLayoutInput, *, build: McpBuild, **kwargs
) -> list[StorageSlot]:
    """Get the storage layout of a Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)

    if (
        contract.compilation_info is None
        or contract.compilation_info.storage_layout is None
    ):
        raise ValueError(
            f"Storage layout is not available for contract {input.contract_name}"
        )

    storage_layout = contract.compilation_info.storage_layout
    result: list[StorageSlot] = []

    # Step 3: Fill List[StorageSlot] with info about storage slots
    for storage_info in storage_layout.storage:
        # Resolve the variable declaration from AST ID
        var = build.reference_resolver.resolve_node(
            storage_info.ast_id,
            contract.source_unit.cu_hash,
        )
        assert isinstance(var, ir.VariableDeclaration)

        # Extract information from the resolved variable
        name = var.name
        type_info = var.type_string

        assert isinstance(var.parent, ir.ContractDefinition)
        variable_contract_name = var.parent.name

        # Create StorageSlot entry
        slot_entry = StorageSlot(
            slot=storage_info.slot,
            offset=storage_info.offset,
            location=node_to_location(var, build),
            name=name,
            type=type_info,
            contract_name=variable_contract_name,
        )

        result.append(slot_entry)

    return result
