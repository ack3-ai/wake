from pydantic import Field

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc, resolve_contract


class GetC3LinearizationInput(ToolInput):
    contract_name: str = Field(
        ..., description="Name of the contract to get C3 linearization for"
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )


@mcp_tool
def get_c3_linearization(
    input: GetC3LinearizationInput, *, build: McpBuild, **kwargs
) -> str:
    """Get the C3 linearization of a Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)

    linearization = contract.linearized_base_contracts
    if not linearization:
        return f"No linearization for {input.contract_name}."

    lines = []
    for i, c in enumerate(linearization, 1):
        kind = (
            "abstract contract" if c.kind == "contract" and c.abstract else c.kind.value
        )
        lines.append(f"{i}. {kind} {c.name} @ {node_loc(c, build)}")
    return (
        f"C3 linearization of {input.contract_name}, most-derived first "
        f"({len(lines)}):\n" + "\n".join(lines)
    )
