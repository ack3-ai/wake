from typing import TypedDict

from pydantic import Field

from ..common import McpBuild
from .common import (
    Location,
    ToolInput,
    mcp_tool,
    node_end_to_location,
    node_to_location,
    resolve_contract,
)


class GetC3LinearizationInput(ToolInput):
    contract_name: str = Field(
        ..., description="Name of the contract to get C3 linearization for"
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )


class ContractInfo(TypedDict):
    name: str
    kind: str
    location: Location
    end_location: Location


@mcp_tool
def get_c3_linearization(
    input: GetC3LinearizationInput, *, build: McpBuild, **kwargs
) -> list[ContractInfo]:
    """Get the C3 linearization of a Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)

    return [
        ContractInfo(
            name=c.name,
            kind=(
                "abstract contract"
                if c.kind == "contract" and c.abstract
                else c.kind.value
            ),
            location=node_to_location(c, build),
            end_location=node_end_to_location(c, build),
        )
        for c in contract.linearized_base_contracts
    ]
