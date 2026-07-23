from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, mcp_tool, resolve_source_unit, source_unit_to_file


class GetDefinitionSourceInput(ToolInput):
    definition_name: str = Field(
        ...,
        description="Name or canonical name of the definition to get source code for",
    )
    file_path: str | None = Field(
        None,
        description="Optional path to the Solidity file where the definition is located",
    )
    number_lines: bool = Field(
        False, description="Whether to include line numbers in the output"
    )


@mcp_tool
def get_definition_source(
    input: GetDefinitionSourceInput, *, build: McpBuild, **kwargs
) -> str:
    """Get the source code of a Solidity definition (contract, struct, enum, error, event, etc.)."""
    if input.file_path is not None:
        source_units = [resolve_source_unit(build, input.file_path)]
    else:
        source_units = build.source_units.values()

    possible_declarations: list[ir.DeclarationAbc] = []

    for source_unit in source_units:
        for declaration in source_unit.declarations_iter():
            if declaration.name == input.definition_name:
                possible_declarations.append(declaration)
            elif declaration.canonical_name == input.definition_name:
                possible_declarations.append(declaration)

    if len(possible_declarations) == 0:
        raise ValueError(f"No declaration named {input.definition_name} found")
    elif len(possible_declarations) > 1:
        if input.file_path is not None:
            raise ValueError(
                f"Multiple declarations named {input.definition_name} found in {input.file_path}, choose one of: {', '.join(d.canonical_name for d in possible_declarations)}"
            )
        else:
            files = [
                source_unit_to_file(build, d.source_unit) for d in possible_declarations
            ]
            raise ValueError(
                f"Multiple declarations named {input.definition_name} found in {', '.join(files)}"
            )
    declaration = possible_declarations[0]
    source_unit = declaration.source_unit

    # TODO: non-natspec documentation support, str natspec support
    if isinstance(
        documentation := getattr(declaration, "documentation", None),
        ir.StructuredDocumentation,
    ):
        start_line, _ = source_unit.get_line_col_from_byte_offset(
            documentation.byte_location[0]
        )
        code = source_unit.file_source[
            documentation.byte_location[0] : declaration.byte_location[1]
        ].decode("utf-8")
    else:
        start_line, _ = source_unit.get_line_col_from_byte_offset(
            declaration.byte_location[0]
        )
        code = declaration.source

    if input.number_lines:
        return "\n".join(
            f"{start_line + i}: {line}" for i, line in enumerate(code.splitlines())
        )
    else:
        return code
