from functools import reduce
from operator import or_

from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import (
    ToolInput,
    mcp_tool,
    node_loc,
    normalize_whitespace,
    overlapping_nodes_at_line,
)


class GetExpressionTypeInput(ToolInput):
    file_path: str = Field(..., description="Path to the Solidity file")
    line: int = Field(
        ...,
        description="Line number where the expression starts (1-based, as shown in editors)",
    )
    expression: str = Field(..., description="Expression to get the type of")


def _get_user_defined_type_info(t: ir.types.TypeAbc) -> set[ir.DeclarationAbc]:
    if isinstance(t, ir.types.Function):
        return reduce(
            or_, (_get_user_defined_type_info(param) for param in t.parameters), set()
        ) | reduce(
            or_,
            (_get_user_defined_type_info(param) for param in t.return_parameters),
            set(),
        )
    elif isinstance(t, ir.types.Tuple):
        return reduce(
            or_,
            (_get_user_defined_type_info(c) for c in t.components if c is not None),
            set(),
        )
    elif isinstance(t, ir.types.Type):
        return _get_user_defined_type_info(t.actual_type)
    elif isinstance(t, ir.types.Modifier):
        return reduce(
            or_, (_get_user_defined_type_info(param) for param in t.parameters), set()
        )
    elif isinstance(t, ir.types.Array):
        return _get_user_defined_type_info(t.base_type)
    elif isinstance(t, ir.types.Mapping):
        return _get_user_defined_type_info(t.key_type) | _get_user_defined_type_info(
            t.value_type
        )
    elif isinstance(t, ir.types.Contract):
        return {t.ir_node}
    elif isinstance(t, ir.types.Struct):
        return reduce(
            or_, (_get_user_defined_type_info(m.type) for m in t.ir_node.members), set()
        ) | {t.ir_node}
    elif isinstance(t, ir.types.Enum):
        return {t.ir_node}
    elif isinstance(t, ir.types.Magic) and t.meta_argument_type is not None:
        return _get_user_defined_type_info(t.meta_argument_type)
    elif isinstance(t, ir.types.UserDefinedValueType):
        return {t.ir_node}
    else:
        return set()


@mcp_tool
def get_expression_type(
    input: GetExpressionTypeInput, *, build: McpBuild, **kwargs
) -> str:
    """Get the type of an expression at a given location in Solidity code."""
    nodes = overlapping_nodes_at_line(build, input.file_path, input.line)
    normalized_expression = normalize_whitespace(input.expression)

    node = next(
        (
            n
            for n in nodes
            if isinstance(n, ir.ExpressionAbc)
            and normalize_whitespace(n.source) == normalized_expression
            or isinstance(n, ir.VariableDeclaration)
            and n.name == normalized_expression
        ),
        None,
    )
    if node is None:
        raise ValueError(
            f"No expression named {input.expression} found at {input.file_path}:{input.line}"
        )

    if node.type is None or node.type_string is None:
        raise ValueError(
            f"Expression {input.expression} at {input.file_path}:{input.line} has no type info"
        )

    # be so kind and provide additional info on user-defined types used in the expression type
    referenced_types = _get_user_defined_type_info(node.type)

    lines = [f"Type of `{input.expression}`: {node.type_string}"]
    if referenced_types:
        lines.append("Referenced user-defined types:")
        for t in sorted(referenced_types, key=lambda t: t.canonical_name):
            lines.append(f"- {t.canonical_name} @ {node_loc(t, build)}")
    return "\n".join(lines)
