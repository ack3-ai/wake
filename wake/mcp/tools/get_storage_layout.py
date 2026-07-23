from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc, resolve_contract


class GetStorageLayoutInput(ToolInput):
    contract_name: str = Field(
        ..., description="Name of the contract to get storage layout for"
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )


@mcp_tool
def get_storage_layout(
    input: GetStorageLayoutInput, *, build: McpBuild, **kwargs
) -> str:
    """Get the storage layout of a Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)

    if (
        contract.compilation_info is None
        or contract.compilation_info.storage_layout is None
    ):
        raise ValueError(
            f"Storage layout is not available for contract {input.contract_name}"
        )

    lines: list[str] = []
    for storage_info in contract.compilation_info.storage_layout.storage:
        var = build.reference_resolver.resolve_node(
            storage_info.ast_id,
            contract.source_unit.cu_hash,
        )
        assert isinstance(var, ir.VariableDeclaration)
        assert isinstance(var.parent, ir.ContractDefinition)
        lines.append(
            f"- slot {storage_info.slot} offset {storage_info.offset}: "
            f"{var.name}: {var.type_string} (declared in {var.parent.name}) "
            f"@ {node_loc(var, build)}"
        )

    if not lines:
        return f"{input.contract_name} has an empty storage layout."
    return (
        f"Storage layout of {input.contract_name} ({len(lines)} slots):\n"
        + "\n".join(lines)
    )
