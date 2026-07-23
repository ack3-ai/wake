import re
from pathlib import Path

from pydantic import Field

from wake.ir import FunctionDefinition
from wake.utils import is_relative_to

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc, resolve_source_unit


class FindFunctionsByRegexInput(ToolInput):
    pattern: str = Field(..., description="Regular expression to match function names")
    file_path: str | None = Field(
        None,
        description="Path to the Solidity file. If not provided, searches all Solidity files",
    )
    contract_name: str | None = Field(
        None,
        description="Name of the contract to search in. If not provided, searches all contracts",
    )


def _param(param) -> str:
    return f"{param.name}: {param.type_string}" if param.name else param.type_string


def _format_function(
    function: FunctionDefinition, contract_name: str | None, build: McpBuild
) -> str:
    owner = f"{contract_name}." if contract_name else ""
    params = ", ".join(_param(p) for p in function.parameters.parameters)
    returns = function.return_parameters.parameters
    rets = f" returns ({', '.join(_param(p) for p in returns)})" if returns else ""
    modifiers = [m.modifier_name.name for m in function.modifiers]
    mods = f", modifiers: {', '.join(modifiers)}" if modifiers else ""
    return (
        f"- {owner}{function.name}({params}){rets} — "
        f"{function.visibility.value} {function.state_mutability.value}{mods} "
        f"@ {node_loc(function, build)}"
    )


@mcp_tool
def find_functions_by_regex(
    input: FindFunctionsByRegexInput, *, build: McpBuild, **kwargs
) -> str:
    """Find functions by regex pattern in a specific Solidity contract."""
    if input.file_path is not None:
        try:
            source_units = [resolve_source_unit(build, input.file_path)]
        except ValueError:
            path = Path(input.file_path).resolve()

            if path.is_dir():
                source_units = [
                    build.source_units[p]
                    for p in build.source_units
                    if is_relative_to(p, path)
                ]
            else:
                raise ValueError(f"File {input.file_path} not found") from None
    else:
        source_units = list(build.source_units.values())

    regex = re.compile(input.pattern)

    lines: list[str] = []

    for source_unit in source_units:
        if input.contract_name is None:
            for function in source_unit.functions:
                if regex.match(function.name):
                    lines.append(_format_function(function, None, build))

        for contract in source_unit.contracts:
            if input.contract_name is None or input.contract_name == contract.name:
                for function in contract.functions:
                    if regex.match(function.name):
                        lines.append(_format_function(function, contract.name, build))

    if not lines:
        return f"No functions match /{input.pattern}/."
    return f"Functions matching /{input.pattern}/ ({len(lines)}):\n" + "\n".join(lines)
