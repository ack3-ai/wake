import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Type, TypedDict, TypeVar

from pydantic import BaseModel

import wake.ir as ir

from ..common import McpBuild

logger = logging.getLogger(__name__)


class ToolInput(BaseModel):
    pass


@dataclass
class ToolDef:
    name: str
    description: str
    input_model: Type[ToolInput]
    handler: Callable


TOOL_REGISTRY: dict[str, ToolDef] = {}


F = TypeVar("F", bound=Callable)


def mcp_tool(func: F) -> F:
    """Decorator to register a function as an MCP tool.

    Infers the input model from the type hint of the first parameter.
    """
    import inspect
    from typing import get_type_hints

    hints = get_type_hints(func)
    first_param = next(iter(inspect.signature(func).parameters))
    input_model = hints[first_param]
    assert issubclass(
        input_model, ToolInput
    ), f"First parameter of {func.__name__} must be a ToolInput subclass"

    name = func.__name__
    description = (func.__doc__ or "").strip()
    TOOL_REGISTRY[name] = ToolDef(
        name=name,
        description=description,
        input_model=input_model,
        handler=func,
    )
    return func


class Location(TypedDict):
    file: str
    line: int
    column: int


# Tools return plain text (not JSON) because their only consumer is an LLM, and
# text is ~55% cheaper in tokens. Since there are no field names, each handler's
# output must be self-describing. Shared conventions across all tools:
#   * A header line names the entity and count, and echoes the relevant input,
#     e.g. "Contracts (4):" or "References to `owner` (3):".
#   * Solidity keywords are emitted bare — they name themselves: contract kinds
#     (interface/library/abstract contract), visibility (public/external/...),
#     mutability (view/payable/mutable/immutable/...).
#   * Locations use "path:line:col" after "@" (see loc_str / node_loc).
#   * Declarations and parameters use "name: type".
#   * A word precedes any value that would otherwise be ambiguous: "slot 0",
#     "offset 0", "line 74", "returns (...)", "modifiers: ...", "used by:".
#   * Empty results say so ("No references found.") rather than a bare header.


def loc_str(location: Location) -> str:
    """Render a location as the universal ``path:line:col`` editor/compiler form."""
    return f"{location['file']}:{location['line']}:{location['column']}"


def node_loc(node: ir.IrAbc, build: McpBuild) -> str:
    """``path:line:col`` for an IR node (shortcut for ``loc_str(node_to_location(...))``)."""
    return loc_str(node_to_location(node, build))


def node_to_location(node: ir.IrAbc, build: McpBuild) -> Location:
    line, col = node.source_unit.get_line_col_from_byte_offset(node.byte_location[0])
    return Location(
        file=source_unit_to_file(build, node.source_unit),
        line=line,
        column=col,
    )


def node_end_to_location(node: ir.IrAbc, build: McpBuild) -> Location:
    line, col = node.source_unit.get_line_col_from_byte_offset(node.byte_location[1])
    return Location(
        file=source_unit_to_file(build, node.source_unit),
        line=line,
        column=col,
    )


def source_unit_to_file(build: McpBuild, source_unit: ir.SourceUnit) -> str:
    relative_root = (
        build.relative_compilation_root
        if build.relative_compilation_root is not None
        else Path("./")
    )
    if source_unit.file.is_relative_to(build.project_root):
        return str(relative_root / source_unit.file.relative_to(build.project_root))
    else:
        return str(source_unit.file)


# this would be implemented in wake.ir.meta.source_unit
# until then we need to implement it here
def get_byte_offset_from_line_col(self: ir.SourceUnit, line: int, col: int) -> int:
    """Convert 1-indexed line and column numbers to byte offset.

    Args:
        line: 1-indexed line number
        col: 1-indexed column number

    Returns:
        byte offset in the file
    """
    if self._lines_index is None:
        self._lines_index = []
        prefix_sum = 0

        for line_content in self._file_source.splitlines(keepends=True):
            self._lines_index.append((line_content, prefix_sum))
            prefix_sum += len(line_content)

    if line < 1 or line > len(self._lines_index):
        raise ValueError(f"Line number {line} is out of range")

    line_content, line_start_offset = self._lines_index[line - 1]

    # Convert UTF-16 column position to UTF-8 bytes
    line_str = line_content.decode("utf-8")
    if col < 1 or col > len(line_str) + 1:
        raise ValueError(f"Column number {col} is out of range for line {line}")

    # Convert the column-indexed substring to UTF-8 bytes
    col_bytes = len(line_str[: col - 1].encode("utf-16-le")) // 2
    utf8_bytes = len(line_str[:col_bytes].encode("utf-8"))

    return line_start_offset + utf8_bytes


def resolve_source_unit(build: McpBuild, file_path: str) -> ir.SourceUnit:
    path = Path(file_path)

    if build.relative_compilation_root is not None:
        source_unit_name = (
            build.relative_compilation_root / Path(file_path)
        ).as_posix()
    else:
        source_unit_name = file_path

    # first try to interpret file_path as a source unit name
    s = next(
        (
            s
            for s in build.source_units.values()
            if source_unit_name in s.source_unit_names
        ),
        None,
    )
    if s is not None:
        return s

    path = path.resolve()

    if build.relative_compilation_root is not None:
        try:
            rel_path = path.relative_to(Path.cwd())
            rel_path = rel_path.relative_to(build.relative_compilation_root)

            final_path = build.project_root / rel_path
            if final_path in build.source_units:
                return build.source_units[final_path]
        except ValueError as e:
            logger.warning(f"Error resolving source unit: {e}")
            pass
    else:
        final_path = path.resolve()

        if final_path in build.source_units:
            return build.source_units[final_path]

    raise ValueError(f"File {path} not found")


def resolve_contract(
    build: McpBuild, contract_name: str, file_path: str | None
) -> ir.ContractDefinition:
    if file_path is not None:
        source_unit = resolve_source_unit(build, file_path)
        contract = next(
            (c for c in source_unit.contracts if c.name == contract_name), None
        )
        if contract is None:
            raise ValueError(f"Contract {contract_name} not found in {file_path}")
    else:
        contracts = [
            c
            for source_unit in build.source_units.values()
            for c in source_unit.contracts
            if c.name == contract_name
        ]
        if len(contracts) == 0:
            raise ValueError(f"Contract {contract_name} not found")
        elif len(contracts) > 1:
            raise ValueError(
                f"Multiple contracts named {contract_name} found in files: {', '.join(source_unit_to_file(build, c.source_unit) for c in contracts)}"
            )
        contract = contracts[0]

    return contract


def resolve_function(
    build: McpBuild,
    contract_name: str,
    function_name: str,
    file_path: str | None,
    search_in_base_contracts: bool,
) -> ir.FunctionDefinition:
    contract = resolve_contract(build, contract_name, file_path)

    ignore_functions: set[ir.FunctionDefinition] = set()
    possible_functions: list[ir.FunctionDefinition] = []

    for c in (
        contract.linearized_base_contracts if search_in_base_contracts else [contract]
    ):
        for function in c.functions:
            if function in ignore_functions:
                ignore_functions.update(function.base_functions)
                continue

            if function.name == function_name:
                possible_functions.append(function)
                ignore_functions.update(function.base_functions)
            elif function.canonical_name == function_name:
                possible_functions.append(function)
                ignore_functions.update(function.base_functions)
            elif (
                "." in function.canonical_name.split("(")[0]
                and function.canonical_name.split(".", 1)[1] == function_name
            ):
                possible_functions.append(function)
                ignore_functions.update(function.base_functions)

    if len(possible_functions) == 0:
        raise ValueError(
            f"Function {function_name} not found in {contract_name} or its base contracts"
        )
    elif len(possible_functions) > 1:
        raise ValueError(
            f"Multiple functions named {function_name} found in {contract_name} and its base contracts, choose one of: {', '.join(f.canonical_name for f in possible_functions)}"
        )
    return possible_functions[0]


def nodes_at_location(
    build: McpBuild, file_path: str, line: int, column: int
) -> tuple[list[ir.IrAbc], int]:
    source_unit = resolve_source_unit(build, file_path)
    byte_offset = get_byte_offset_from_line_col(source_unit, line, column)

    interval_tree = build.interval_trees[source_unit.file]
    intervals = interval_tree.at(byte_offset)
    nodes: list[ir.IrAbc] = [interval.data for interval in intervals]
    return nodes, byte_offset


def overlapping_nodes_at_line(
    build: McpBuild, file_path: str, line: int
) -> list[ir.IrAbc]:
    source_unit = resolve_source_unit(build, file_path)
    if source_unit._lines_index is None:
        source_unit._lines_index = []
        prefix_sum = 0

        for line_content in source_unit._file_source.splitlines(keepends=True):
            source_unit._lines_index.append((line_content, prefix_sum))
            prefix_sum += len(line_content)

    if line < 1 or line > len(source_unit._lines_index):
        raise ValueError(f"Line number {line} is out of range")

    line_content, line_start_offset = source_unit._lines_index[line - 1]

    interval_tree = build.interval_trees[source_unit.file]
    nodes = interval_tree.overlap(
        line_start_offset, line_start_offset + len(line_content)
    )
    return [interval.data for interval in nodes]


def normalize_whitespace(text: str) -> str:
    """Normalize whitespace in text for comparison purposes."""
    # Remove all whitespace for comparison
    return re.sub(r"\s+", "", text)
