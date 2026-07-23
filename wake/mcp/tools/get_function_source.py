from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, mcp_tool, resolve_function


class GetFunctionSourceInput(ToolInput):
    contract_name: str = Field(
        ..., description="Name of the contract containing the function"
    )
    function_name: str = Field(
        ..., description="Name or canonical name of the function to get source code for"
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the contract is defined",
    )
    search_in_base_contracts: bool = Field(
        False, description="Whether to search in base contracts for the function"
    )
    number_lines: bool = Field(
        False, description="Whether to include line numbers in the output"
    )


@mcp_tool
def get_function_source(
    input: GetFunctionSourceInput, *, build: McpBuild, **kwargs
) -> str:
    """Get the source code of a function in a Solidity contract."""
    function = resolve_function(
        build,
        input.contract_name,
        input.function_name,
        input.file_path,
        input.search_in_base_contracts,
    )
    source_unit = function.source_unit

    # TODO: non-natspec documentation support, str natspec support
    if isinstance(function.documentation, ir.StructuredDocumentation):
        start_line, _ = source_unit.get_line_col_from_byte_offset(
            function.documentation.byte_location[0]
        )
        code = source_unit.file_source[
            function.documentation.byte_location[0] : function.byte_location[1]
        ].decode("utf-8")
    else:
        start_line, _ = source_unit.get_line_col_from_byte_offset(
            function.byte_location[0]
        )
        code = function.source

    if input.number_lines:
        return "\n".join(
            f"{start_line + i}: {line}" for i, line in enumerate(code.splitlines())
        )
    else:
        return code
