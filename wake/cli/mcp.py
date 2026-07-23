from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import rich_click as click


@click.command(name="mcp")
@click.option(
    "--http",
    "http_port",
    type=int,
    default=None,
    metavar="PORT",
    help="Serve over streamable HTTP on PORT. If not given, serve over stdio.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to (HTTP transport only).",
    show_default=True,
)
@click.option(
    "--import-json",
    "import_json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Compile once from a sources.json exported by 'wake open --export json' "
    "instead of dynamically compiling (and watching) the current project.",
)
@click.pass_context
def run_mcp(
    context: click.Context,
    http_port: Optional[int],
    host: str,
    import_json: Optional[Path],
) -> None:
    """
    Start the MCP (Model Context Protocol) server.

    By default the server serves over stdio and dynamically compiles the
    Solidity project in the current directory, recompiling on file changes.
    Pass --http PORT to serve over streamable HTTP instead, or --import-json
    to compile once from a previously exported sources.json.
    """
    from wake.mcp import run_http, run_stdio

    local_config_path = context.obj.get("local_config_path", None)

    if http_port is not None:
        asyncio.run(
            run_http(
                host=host,
                port=http_port,
                json_file=import_json,
                local_config_path=local_config_path,
            )
        )
    else:
        asyncio.run(
            run_stdio(
                json_file=import_json,
                local_config_path=local_config_path,
            )
        )
