import logging
from typing import TypedDict

from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, mcp_tool, source_unit_to_file

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class FindExternalCallsBySelectorInput(ToolInput):
    selector: str = Field(..., description="Function selector of the external call")


class ExternalCall(TypedDict):
    file_path: str
    line: int
    column: int


def _extract_selectors(node: ir.ExpressionAbc) -> set[bytes]:
    if (
        isinstance(node, ir.TupleExpression)
        and len(node.components) == 1
        and node.components[0] is not None
    ):
        return _extract_selectors(node.components[0])
    elif isinstance(node, (ir.MemberAccess, ir.Identifier)):
        ref_decl = node.referenced_declaration
        if (
            isinstance(ref_decl, (ir.FunctionDefinition, ir.VariableDeclaration))
            and ref_decl.function_selector is not None
        ):
            return {ref_decl.function_selector}
        return set()
    elif isinstance(node, ir.FunctionCall):
        node = node.expression
        if isinstance(node, ir.MemberAccess) and node.referenced_declaration in {
            ir.enums.GlobalSymbol.FUNCTION_VALUE,
            ir.enums.GlobalSymbol.FUNCTION_GAS,
        }:
            return _extract_selectors(node.expression)
        return set()
    elif isinstance(node, ir.FunctionCallOptions):
        return _extract_selectors(node.expression)
    elif isinstance(node, ir.Conditional):
        return _extract_selectors(node.true_expression) | _extract_selectors(
            node.false_expression
        )
    else:
        return set()


@mcp_tool
def find_external_calls_by_selector(
    input: FindExternalCallsBySelectorInput, *, build: McpBuild, **kwargs
) -> list[ExternalCall]:
    """Find external calls by selector in a specific Solidity contract."""
    selector_str = input.selector
    if selector_str.startswith("0x"):
        selector_str = selector_str[2:]
    selector = bytes.fromhex(selector_str)

    external_calls = []
    for source_unit in build.source_units.values():
        for node in source_unit:
            if (
                not isinstance(node, ir.FunctionCall)
                or node.kind != ir.enums.FunctionCallKind.FUNCTION_CALL
            ):
                continue

            if (
                not isinstance(node.expression.type, ir.types.Function)
                or node.expression.type.kind != ir.enums.FunctionTypeKind.EXTERNAL
            ):
                continue

            extracted_selectors = _extract_selectors(node.expression)
            if selector not in extracted_selectors:
                continue

            line, col = source_unit.get_line_col_from_byte_offset(node.byte_location[0])
            external_calls.append(
                {
                    "file_path": source_unit_to_file(build, source_unit),
                    "line": line,
                    "column": col,
                }
            )

    return external_calls
