from typing import Literal, TypedDict

from pydantic import Field

import wake.ir as ir
from wake.analysis.utils import get_all_base_and_child_declarations

from ..common import McpBuild
from .common import (
    Location,
    ToolInput,
    loc_str,
    mcp_tool,
    node_to_location,
    normalize_whitespace,
    overlapping_nodes_at_line,
)


class GoToDefinitionInput(ToolInput):
    file_path: str = Field(..., description="Path to the Solidity file")
    line: int = Field(
        ...,
        description="Line number where the state variable is declared (1-based, as shown in editors)",
    )
    expression: str = Field(
        ...,
        description="Expression to go to the definition of. If it's a member, pass the full expression",
    )


class Definition(TypedDict):
    location: Location
    kind: Literal["implementation", "declaration"]


def _ir_to_location(node: ir.DeclarationAbc, build: McpBuild) -> list[Definition]:
    result: list[Definition] = []

    if isinstance(
        node, (ir.FunctionDefinition, ir.ModifierDefinition, ir.VariableDeclaration)
    ):
        for decl in get_all_base_and_child_declarations(node):
            if not isinstance(decl, (ir.VariableDeclaration)):
                result.append(
                    Definition(
                        location=node_to_location(decl, build),
                        kind="implementation" if decl.implemented else "declaration",
                    )
                )
            else:
                result.append(
                    Definition(
                        location=node_to_location(decl, build),
                        kind="declaration",
                    )
                )
    else:
        return [
            Definition(
                location=node_to_location(node, build),
                kind="declaration",
            )
        ]

    return result


def _collect_definitions(
    input: GoToDefinitionInput, build: McpBuild
) -> list[Definition]:
    nodes = overlapping_nodes_at_line(build, input.file_path, input.line)
    normalized_expression = normalize_whitespace(input.expression)

    node = None
    for n in nodes:
        if (
            isinstance(n, (ir.ExpressionAbc, ir.IdentifierPath, ir.UserDefinedTypeName))
            and normalize_whitespace(n.source) == normalized_expression
        ):
            node = n
            break

        if (
            isinstance(n, ir.MemberAccess)
            and normalize_whitespace(n.member_name) == normalized_expression
        ):
            node = n
            break

        if isinstance(n, (ir.IdentifierPath, ir.UserDefinedTypeName)):
            for part in n.identifier_path_parts:
                source = n.source_unit.file_source[
                    part.byte_location[0] : part.byte_location[1]
                ].decode("utf-8")
                if (
                    normalize_whitespace(source) == normalized_expression
                    or normalize_whitespace(part.name) == normalized_expression
                ):
                    node = n
                    break
            if node is not None:
                break

    if node is None:
        raise ValueError(
            f"No expression named {input.expression} found at {input.file_path}:{input.line}"
        )

    if isinstance(node, ir.YulIdentifier):
        if node.external_reference is None:
            raise ValueError(
                f"Identifier at {input.file_path}:{input.line} is not a Solidity reference"
            )
        return _ir_to_location(node.external_reference.referenced_declaration, build)
    elif isinstance(
        node,
        (
            ir.Identifier,
            ir.MemberAccess,
            ir.IdentifierPath,
            ir.UserDefinedTypeName,
            ir.IdentifierPathPart,
        ),
    ):
        decl = node.referenced_declaration
        if isinstance(decl, ir.enums.GlobalSymbol):
            raise ValueError(
                f"Identifier at {input.file_path}:{input.line} references a global symbol {repr(decl)}"
            )
        elif isinstance(decl, ir.SourceUnit):
            raise ValueError(
                f"Identifier at {input.file_path}:{input.line} references a source unit"
            )

        if isinstance(decl, set):
            result = set()
            for d in decl:
                result.update(_ir_to_location(d, build))
            return list(result)
        else:
            return _ir_to_location(decl, build)
    else:
        raise ValueError(
            f"No valid expression named `{input.expression}` found at {input.file_path}:{input.line}"
        )


@mcp_tool
def go_to_definition(input: GoToDefinitionInput, *, build: McpBuild, **kwargs) -> str:
    """Go to the definition of a specific identifier at a given location in Solidity code."""
    definitions = _collect_definitions(input, build)
    if not definitions:
        return f"No definition found for `{input.expression}`."
    body = "\n".join(f"- {d['kind']} @ {loc_str(d['location'])}" for d in definitions)
    return f"Definition of `{input.expression}` ({len(definitions)}):\n{body}"
