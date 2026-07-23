from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, mcp_tool, resolve_contract


class GetContractSourceInput(ToolInput):
    contract_name: str = Field(
        ..., description="Name of the contract to get source code for"
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )
    number_lines: bool = Field(
        False, description="Whether to include line numbers in the output"
    )


@mcp_tool
def get_contract_source(
    input: GetContractSourceInput, *, build: McpBuild, **kwargs
) -> str:
    """Get the source code of a Solidity contract."""
    contract = resolve_contract(build, input.contract_name, input.file_path)
    source_unit = contract.source_unit

    # TODO: non-natspec documentation support, str natspec support
    if isinstance(contract.documentation, ir.StructuredDocumentation):
        start_line, _ = source_unit.get_line_col_from_byte_offset(
            contract.documentation.byte_location[0]
        )
        code = source_unit.file_source[
            contract.documentation.byte_location[0] : contract.byte_location[1]
        ].decode("utf-8")
    else:
        start_line, _ = source_unit.get_line_col_from_byte_offset(
            contract.byte_location[0]
        )
        code = contract.source

    if input.number_lines:
        return "\n".join(
            f"{start_line + i}: {line}" for i, line in enumerate(code.splitlines())
        )
    else:
        return code
