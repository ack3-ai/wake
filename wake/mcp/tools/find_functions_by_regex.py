import re
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import Field

from wake.ir import FunctionDefinition
from wake.utils import is_relative_to

from ..common import McpBuild
from .common import Location, ToolInput, mcp_tool, node_to_location, resolve_source_unit


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


class Modifier(TypedDict):
    name: str
    location: Location


class Parameter(TypedDict):
    name: str
    type: str


class Function(TypedDict):
    function_name: str
    contract_name: str | None
    location: Location
    visibility: Literal["public", "internal", "private", "external"]
    state_mutability: Literal["payable", "nonpayable", "view", "pure"]
    modifiers: list[Modifier]
    parameters: list[Parameter]
    returns: list[Parameter]


def _process_function(
    function: FunctionDefinition, contract_name: str | None, build: McpBuild
) -> Function:
    return Function(
        function_name=function.name,
        contract_name=contract_name,
        location=node_to_location(function, build),
        visibility=function.visibility.value,
        state_mutability=function.state_mutability.value,
        modifiers=[
            Modifier(
                name=modifier.modifier_name.name,
                location=node_to_location(modifier, build),
            )
            for modifier in function.modifiers
        ],
        parameters=[
            Parameter(
                name=param.name,
                type=param.type_string,
            )
            for param in function.parameters.parameters
        ],
        returns=[
            Parameter(
                name=param.name,
                type=param.type_string,
            )
            for param in function.return_parameters.parameters
        ],
    )


@mcp_tool
def find_functions_by_regex(
    input: FindFunctionsByRegexInput, *, build: McpBuild, **kwargs
) -> list[Function]:
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

    result = []

    for source_unit in source_units:
        if input.contract_name is None:
            for function in source_unit.functions:
                if regex.match(function.name):
                    result.append(_process_function(function, None, build))

        for contract in source_unit.contracts:
            if input.contract_name is None or input.contract_name == contract.name:
                for function in contract.functions:
                    if regex.match(function.name):
                        result.append(_process_function(function, contract.name, build))

    return result
