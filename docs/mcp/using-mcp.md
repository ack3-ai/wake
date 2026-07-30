# Using the MCP server

Wake implements an [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) server that exposes its static-analysis capabilities as tools for LLM agents and AI coding assistants. It lets a model explore and reason about a Solidity codebase — list contracts, resolve declarations, read sources, follow references, inspect storage layout, and more — without leaving the assistant.

The server compiles the project with Wake and answers tool calls against the compiled [IR](../api-reference/ir/abc.md). All analysis is static; no chain state, RPC, or deployment is involved.

## Running the server

Start the server over the default stdio transport with:

```shell
wake mcp
```

By default the server dynamically compiles the Solidity project in the current directory and recompiles on file changes.

### Transports

The server supports two transports:

=== "stdio (default)"

    ```shell
    wake mcp
    ```

    Communicates over standard input/output using newline-delimited JSON-RPC. This is the transport most MCP clients (AI assistants, editors) launch and manage themselves.

    !!! warning
        On the stdio transport, standard output is reserved for the protocol. Wake automatically routes its logging to standard error so it cannot corrupt the message stream.

=== "streamable HTTP"

    ```shell
    wake mcp --http 8000
    ```

    Serves the streamable HTTP transport on the given port. Use `--host` to change the bind address (default `127.0.0.1`):

    ```shell
    wake mcp --http 8000 --host 0.0.0.0
    ```

    The following endpoints are exposed:

    | Endpoint    | Description                                                            |
    |-------------|-----------------------------------------------------------------------|
    | `/mcp`      | The MCP streamable HTTP endpoint clients connect to.                  |
    | `/healthz`  | Liveness probe. Returns `200` with `{"status": "ok"}`.               |
    | `/readyz`   | Readiness probe. `200` once the first compilation finishes, `503` while still compiling. |

### Options

| Command-line name | Type     | Default     | Description                                                                                                              |
|-------------------|----------|-------------|------------------------------------------------------------------------------------------------------------------------|
| `--http`          | `PORT`   | –           | Serve over streamable HTTP on `PORT`. If not given, serve over stdio.                                                   |
| `--host`          | `str`    | `127.0.0.1` | Host to bind to (HTTP transport only).                                                                                  |
| `--import-json`   | `FILE`   | –           | Compile once from a `sources.json` exported by `wake open --export json` instead of dynamically compiling the project. |

The server also honors the global `--config` option and loads `wake.toml` like the rest of Wake. See [Configuration](../configuration.md) for the available options.

!!! info
    On startup the server begins compiling in the background. Tools that need the compiled project wait until the first compilation finishes before returning; connectivity-only calls (such as `echo`) respond immediately.

### Analyzing an exported project

Instead of compiling the working directory, the server can compile once from a project exported with `wake open --export json` (for example a contract fetched from a block explorer):

```shell
wake mcp --import-json .wake/sources.json
```

In this mode the project is compiled a single time from the export and the file watcher is not started.

## Connecting a client

### stdio

Most AI assistants and editors launch the server themselves. Point the client at the `wake mcp` command and set the working directory to your project root:

```json title="MCP client configuration"
{
  "mcpServers": {
    "wake": {
      "command": "wake",
      "args": ["mcp"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

### streamable HTTP

Start the server with `wake mcp --http 8000` and configure the client with the endpoint URL:

```
http://127.0.0.1:8000/mcp
```

## Output format

Tool results are returned as compact, human-readable text rather than JSON. Because the consumer is a language model, plain text is significantly cheaper in tokens than JSON with no loss of information, and each result is self-describing (a header naming the entity and count, `path:line:col` locations, and labels for any otherwise-ambiguous value).

## Next steps

- [Built-in tools](tools.md) — the tools the server exposes.
- [Writing custom tools](writing-tools.md) — extend the server with your own tools.

## Debugging

The server can be run with additional logging using:

```shell
wake --debug mcp
```
