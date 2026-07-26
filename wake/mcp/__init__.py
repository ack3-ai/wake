"""Wake MCP (Model Context Protocol) module."""

from .api import (
    Location,
    McpBuild,
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
from .server import run_http, run_stdio

__all__ = [
    "run_http",
    "run_stdio",
    # public plugin API (see wake.mcp.api)
    "mcp_tool",
    "ToolInput",
    "McpBuild",
    "Location",
    "loc_str",
    "node_loc",
    "node_to_location",
    "node_end_to_location",
    "source_unit_to_file",
    "resolve_source_unit",
    "resolve_contract",
    "resolve_function",
    "nodes_at_location",
    "overlapping_nodes_at_line",
    "normalize_whitespace",
]
