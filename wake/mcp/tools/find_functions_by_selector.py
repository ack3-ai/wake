from pydantic import Field

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc


class FindFunctionsBySelectorInput(ToolInput):
    selector: str = Field(..., description="Function selector to search for")


@mcp_tool
def find_functions_by_selector(
    input: FindFunctionsBySelectorInput, *, build: McpBuild, **kwargs
) -> str:
    """Find functions by selector in all Solidity contracts."""
    selector_str = input.selector
    if selector_str.startswith("0x"):
        selector_str = selector_str[2:]
    selector = bytes.fromhex(selector_str)

    lines: list[str] = []
    for source_unit in build.source_units.values():
        for contract in source_unit.contracts:
            for function in contract.functions:
                if function.function_selector == selector:
                    impl = "implemented" if function.implemented else "unimplemented"
                    lines.append(
                        f"- {contract.name}.{function.name} — {function.visibility} "
                        f"{function.state_mutability}, {impl} @ {node_loc(function, build)}"
                    )

            for variable in contract.declared_variables:
                if variable.function_selector == selector:
                    # public state variables expose an implicit view getter
                    lines.append(
                        f"- {contract.name}.{variable.name} — {variable.visibility} "
                        f"view, implemented @ {node_loc(variable, build)}"
                    )

    if not lines:
        return f"No functions with selector {input.selector}."
    return f"Functions with selector {input.selector} ({len(lines)}):\n" + "\n".join(
        lines
    )
