import logging
from importlib.metadata import entry_points

# Import all built-in tool modules to trigger @mcp_tool registration
from . import (  # noqa: F401
    analyze_state_variables,
    find_external_calls_by_selector,
    find_functions_by_regex,
    find_functions_by_selector,
    find_references,
    get_c3_linearization,
    get_contract_source,
    get_definition_source,
    get_expression_type,
    get_function_source,
    get_state_changes,
    get_storage_layout,
    go_to_definition,
    is_known_contract,
    list_contract_functions,
    list_contracts,
    list_modifiers,
)

logger = logging.getLogger(__name__)

#: Entry-point group third-party packages use to register custom MCP tools.
#: A plugin declares e.g. in its pyproject.toml
#:     [project.entry-points."wake.plugins.mcp"]
#:     my_tools = "my_package.mcp_tools"
#: pointing at a module whose import runs @mcp_tool decorators.
PLUGIN_ENTRY_POINT_GROUP = "wake.plugins.mcp"

_plugins_loaded = False


def load_plugin_tools(*, force: bool = False) -> list[tuple[str, Exception]]:
    """Discover and load third-party MCP tools via the ``wake.plugins.mcp`` entry points.

    Loading an entry point imports its target module, which runs the module's
    ``@mcp_tool`` decorators and thereby populates ``TOOL_REGISTRY``. A plugin
    that fails to import is skipped and reported rather than aborting startup, so
    one broken plugin cannot take down the server.

    Idempotent: only loads once unless ``force`` is set. Returns
    ``(entry_point_name, exception)`` tuples for the plugins that failed to load.
    """
    global _plugins_loaded
    if _plugins_loaded and not force:
        return []

    failed: list[tuple[str, Exception]] = []
    for entry_point in sorted(
        entry_points().select(group=PLUGIN_ENTRY_POINT_GROUP), key=lambda e: e.name
    ):
        try:
            entry_point.load()
        except Exception as e:  # noqa: BLE001 — one bad plugin must not break the server
            logger.warning("Failed to load MCP tool plugin %r: %s", entry_point.name, e)
            failed.append((entry_point.name, e))

    _plugins_loaded = True
    return failed
