from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import ToolInput, loc_str, mcp_tool, node_loc, node_to_location


class ListModifiersInput(ToolInput):
    contract_name: str | None = Field(
        None, description="Optional name of the contract to list modifiers for"
    )


@mcp_tool
def list_modifiers(input: ListModifiersInput, *, build: McpBuild, **kwargs) -> str:
    """List all modifiers in all Solidity contracts and their invocations."""
    blocks: list[str] = []

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

                block = [f"- {modifier.canonical_name} @ {node_loc(modifier, build)}"]
                if invocations:
                    inv_locs = [
                        (node_to_location(inv, build), inv) for inv in invocations
                    ]
                    inv_locs.sort(key=lambda t: (t[0]["file"], t[0]["line"]))
                    block.append("  used by:")
                    for loc, inv in inv_locs:
                        block.append(f"  - {inv.canonical_name} @ {loc_str(loc)}")
                else:
                    block.append("  used by: (none)")
                blocks.append("\n".join(block))

    if not blocks:
        return "No modifiers found."
    return f"Modifiers ({len(blocks)}):\n" + "\n".join(blocks)
