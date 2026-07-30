# Writing custom tools

The MCP server can be extended with custom tools contributed by third-party packages, the same way Wake supports custom [detectors and printers](../static-analysis/getting-started.md). A plugin registers one or more tools under the `wake.plugins.mcp` entry-point group; the server discovers and loads them at startup.

!!! tip
    The built-in [tools](https://github.com/Ackee-Blockchain/wake/tree/main/wake/mcp/tools) are a good starting point for writing your own.

## Tool structure

A tool is a function decorated with `@mcp_tool`. Its first parameter is a `ToolInput` (a [pydantic](https://docs.pydantic.dev/) model) subclass whose fields become the tool's JSON input schema; the function returns the result as a string.

```python
from pydantic import Field

from wake.mcp import McpBuild, ToolInput, mcp_tool, node_loc, resolve_contract


class ContractLocationInput(ToolInput):
    contract_name: str = Field(..., description="Contract to locate")


@mcp_tool
def contract_location(
    input: ContractLocationInput, *, build: McpBuild, compilation_root, **kwargs
) -> str:
    """Report where a contract is defined."""
    contract = resolve_contract(build, input.contract_name, None)
    return f"{contract.name} is defined at {node_loc(contract, build)}"
```

The tool name is the function name (`contract_location` above), and the tool description is taken from the function's docstring.

### Handler contract

The handler is called with keyword arguments and must follow this stable contract:

- The **first parameter** is a `ToolInput` subclass. Its pydantic fields (with their `description`s) are exposed to the model as the tool's input schema.
- It is called with keyword arguments — currently `build` (an [`McpBuild`](#the-mcpbuild-object)) and `compilation_root` (a `Path`) — and **must accept `**kwargs`** so future versions of Wake can pass additional context without breaking the plugin.
- It **returns a `str`** — the human-readable tool result.

!!! important
    Always accept `**kwargs`. This is the mechanism that keeps the plugin API forward-compatible: Wake can add new keyword arguments to the handler call over time, and tools that accept `**kwargs` keep working unchanged.

### The `McpBuild` object

`build` gives access to the statically compiled project:

| Attribute                    | Description                                                        |
|------------------------------|-------------------------------------------------------------------|
| `source_units`               | `dict` mapping file `Path` to a `wake.ir.SourceUnit`.             |
| `interval_trees`             | `dict` mapping file `Path` to an interval tree (byte offset → IR node). |
| `reference_resolver`         | Resolver used to map AST identifiers to IR nodes.                 |
| `project_root`               | Root of the compiled project.                                    |
| `relative_compilation_root`  | Compilation root relative to the working directory, or `None`.    |

From the source units you have the full [`wake.ir`](../api-reference/ir/abc.md) tree, and you can use [`wake.analysis`](../api-reference/analysis/cfg.md) helpers, exactly like detectors and printers do.

## Public API

Everything needed to author a tool is importable from `wake.mcp`:

| Symbol                                              | Purpose                                                        |
|-----------------------------------------------------|---------------------------------------------------------------|
| `mcp_tool`                                          | Decorator that registers a function as a tool.               |
| `ToolInput`                                         | Base class for a tool's pydantic input model.               |
| `McpBuild`, `Location`                              | The build object and the location type.                     |
| `resolve_contract`, `resolve_function`, `resolve_source_unit` | Look up declarations by name / path.               |
| `nodes_at_location`, `overlapping_nodes_at_line`    | Find IR nodes at a source position.                         |
| `node_loc`, `loc_str`, `node_to_location`, `node_end_to_location`, `source_unit_to_file` | Format source locations.        |
| `normalize_whitespace`                              | Whitespace-insensitive text comparison helper.              |

## Registering the plugin

Declare the entry point in your package's `pyproject.toml`, pointing at the module that defines the tools:

```toml title="pyproject.toml"
[project.entry-points."wake.plugins.mcp"]
my_tools = "my_package.mcp_tools"
```

Once the package is installed in the same environment as Wake, its tools are picked up automatically the next time `wake mcp` starts.

## Loading behavior

- Plugins are discovered from **installed packages** declaring the `wake.plugins.mcp` entry-point group and loaded once at server startup. Unlike detectors and printers, project-local (`./`) and global directories are **not** scanned.
- A plugin that fails to import is logged and skipped, so a single broken plugin cannot take down the server.
- Tool names must be unique. If a plugin registers a name that already exists, a warning is emitted and the later registration wins.

## Output conventions

Tool results are plain text, not JSON — the consumer is a language model, and text is far cheaper in tokens. Because there are no field names, make each result self-describing, following the same conventions as the built-in tools:

- Start with a header naming the entity and count, e.g. `Contracts (4):`.
- Emit Solidity keywords bare (they name themselves), and render locations as `path:line:col`.
- Precede any otherwise-ambiguous value with a label (`slot 0`, `returns (...)`, `modifiers: ...`).
- Say so explicitly when there is nothing to report (`No references found.`).
