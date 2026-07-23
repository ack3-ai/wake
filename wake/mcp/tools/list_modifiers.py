from typing import TypedDict

from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import Location, ToolInput, mcp_tool, node_to_location


class ListModifiersInput(ToolInput):
    contract_name: str | None = Field(
        None, description="Optional name of the contract to list modifiers for"
    )


class ModifierInvocation(TypedDict):
    function_name: str
    location: Location


class ModifierUsage(TypedDict):
    name: str
    location: Location
    invocations: list[ModifierInvocation]


@mcp_tool
def list_modifiers(
    input: ListModifiersInput, *, build: McpBuild, **kwargs
) -> list[ModifierUsage]:
    """List all modifiers in all Solidity contracts and their invocations."""
    result: list[ModifierUsage] = []

    for source_unit in build.source_units.values():
        for contract in source_unit.contracts:
            if input.contract_name is not None and contract.name != input.contract_name:
                continue

            for modifier in contract.modifiers:
                invocations: set[ir.FunctionDefinition] = set()

                for ref in modifier.references:
                    if isinstance(ref, ir.IdentifierPathPart):
                        ref = ref.underlying_node
                    elif isinstance(ref, ir.ExternalReference):
                        # should not happen
                        continue
                    p = ref.parent
                    if not isinstance(p, ir.ModifierInvocation):
                        # should not happen
                        continue

                    invocations.add(p.parent)

                result.append(
                    ModifierUsage(
                        name=modifier.canonical_name,
                        location=node_to_location(modifier, build),
                        invocations=sorted(
                            [
                                ModifierInvocation(
                                    function_name=invocation.canonical_name,
                                    location=node_to_location(invocation, build),
                                )
                                for invocation in invocations
                            ],
                            key=lambda x: (
                                x["location"]["file"],
                                x["location"]["line"],
                            ),
                        ),
                    )
                )

    return result
