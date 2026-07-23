from pydantic import Field

from wake.utils.known_contracts import KNOWN_CONTRACTS, compute_code_checksum

from ..common import McpBuild
from .common import ToolInput, mcp_tool, resolve_contract


class IsKnownContractInput(ToolInput):
    contract_name: str = Field(..., description="Name of the contract to check")
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )


@mcp_tool
def is_known_contract(
    input: IsKnownContractInput, *, build: McpBuild, **kwargs
) -> bool:
    """Check if a contract is known."""
    contract = resolve_contract(build, input.contract_name, input.file_path)
    checksum = compute_code_checksum(contract.source)

    return checksum in KNOWN_CONTRACTS
