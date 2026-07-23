from typing import Literal, TypedDict

from pydantic import Field

from ..common import McpBuild
from .common import Location, ToolInput, mcp_tool, node_to_location


class FindFunctionsBySelectorInput(ToolInput):
    selector: str = Field(..., description="Function selector to search for")


class Function(TypedDict):
    function_name: str
    contract_name: str | None
    location: Location
    visibility: Literal["public", "internal", "private", "external"]
    state_mutability: Literal["payable", "nonpayable", "view", "pure"]
    implemented: bool


@mcp_tool
def find_functions_by_selector(
    input: FindFunctionsBySelectorInput, *, build: McpBuild, **kwargs
) -> list[Function]:
    """Find functions by selector in all Solidity contracts."""
    selector_str = input.selector
    if selector_str.startswith("0x"):
        selector_str = selector_str[2:]
    selector = bytes.fromhex(selector_str)

    functions = []
    for source_unit in build.source_units.values():
        for contract in source_unit.contracts:
            for function in contract.functions:
                if function.function_selector == selector:
                    functions.append(
                        {
                            "function_name": function.name,
                            "contract_name": contract.name,
                            "location": node_to_location(function, build),
                            "visibility": function.visibility,
                            "state_mutability": function.state_mutability,
                            "implemented": function.implemented,
                        }
                    )

            for variable in contract.declared_variables:
                if variable.function_selector == selector:
                    functions.append(
                        {
                            "function_name": variable.name,
                            "contract_name": contract.name,
                            "location": node_to_location(variable, build),
                            "visibility": variable.visibility,
                            "state_mutability": "view",
                            "implemented": True,
                        }
                    )

    return functions
