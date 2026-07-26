"""Public API for third-party Wake MCP tool plugins.

A plugin package registers custom tools under the ``wake.plugins.mcp``
entry-point group. In its ``pyproject.toml``::

    [project.entry-points."wake.plugins.mcp"]
    my_tools = "my_package.mcp_tools"

The referenced module is imported at ``wake mcp`` startup and defines tools with
the :func:`mcp_tool` decorator::

    from pydantic import Field

    from wake.mcp import McpBuild, ToolInput, mcp_tool, node_loc, resolve_contract

    class MyInput(ToolInput):
        contract_name: str = Field(..., description="Contract to inspect")

    @mcp_tool
    def my_tool(input: MyInput, *, build: McpBuild, compilation_root, **kwargs) -> str:
        '''One-line description shown to the model.'''
        contract = resolve_contract(build, input.contract_name, None)
        return f"{contract.name} @ {node_loc(contract, build)}"

Stable handler contract:

* The first parameter is a :class:`ToolInput` subclass; its pydantic fields
  become the tool's JSON input schema.
* The handler is called with keyword arguments (currently ``build`` and
  ``compilation_root``) and **must** accept ``**kwargs`` so Wake can pass
  additional context in future versions without breaking the plugin.
* The handler returns a plain ``str`` — the human-readable tool result.

Everything needed to author a tool is re-exported here (and from ``wake.mcp``);
the ``wake.mcp.common`` / ``wake.mcp.tools.common`` modules are implementation
details and should not be imported directly by plugins.
"""

from .common import McpBuild
from .tools.common import (
    Location,
    ToolInput,
    loc_str,
    mcp_tool,
    node_end_to_location,
    node_loc,
    node_to_location,
    nodes_at_location,
    normalize_whitespace,
    overlapping_nodes_at_line,
    resolve_contract,
    resolve_function,
    resolve_source_unit,
    source_unit_to_file,
)

__all__ = [
    # tool authoring
    "mcp_tool",
    "ToolInput",
    "McpBuild",
    "Location",
    # location helpers
    "loc_str",
    "node_loc",
    "node_to_location",
    "node_end_to_location",
    "source_unit_to_file",
    # resolvers / lookups
    "resolve_source_unit",
    "resolve_contract",
    "resolve_function",
    "nodes_at_location",
    "overlapping_nodes_at_line",
    "normalize_whitespace",
]
