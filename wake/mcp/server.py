"""Wake MCP (Model Context Protocol) server."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Optional, Sequence

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import Field

from . import tools  # noqa: F401 — triggers @mcp_tool registration
from .dynamic_compiler import DynamicCompiler
from .json_compiler import JsonCompiler
from .tools.common import TOOL_REGISTRY, ToolInput

logger = logging.getLogger(__name__)


class EchoInput(ToolInput):
    message: str = Field(..., description="Message to echo back")


server = Server("wake-mcp")
compiler: JsonCompiler | DynamicCompiler


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    result = [
        Tool(
            name="echo",
            description="Echo back the provided message",
            inputSchema=EchoInput.model_json_schema(),
        ),
    ]
    for td in TOOL_REGISTRY.values():
        result.append(
            Tool(
                name=td.name,
                description=td.description,
                inputSchema=td.input_model.model_json_schema(),
            )
        )
    return result


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Handle tool calls."""
    if name == "echo":
        input = EchoInput.model_validate(arguments)
        return [TextContent(type="text", text=f"Echo: {input.message}")]

    td = TOOL_REGISTRY.get(name)
    if td is None:
        raise ValueError(f"Unknown tool: {name}")

    input = td.input_model.model_validate(arguments)
    build = await compiler.get_build()
    config = await compiler.get_config()

    # Handlers return the final human-readable text directly.
    text = td.handler(
        input,
        build=build,
        compilation_root=config.project_root_path,
    )
    return [TextContent(type="text", text=text)]


def _init_compiler(
    json_file: Optional[Path], local_config_path: Optional[Path]
) -> JsonCompiler | DynamicCompiler:
    if json_file is not None:
        return JsonCompiler(json_file, Path.cwd(), logger)
    return DynamicCompiler(Path.cwd(), logger, local_config_path=local_config_path)


def _redirect_logging_off_stdout() -> None:
    """Move logging off stdout, which the stdio transport reserves for JSON-RPC.

    Any log line written to stdout corrupts the message stream, so repoint
    stdout-bound handlers (e.g. the ``RichHandler`` set up by the wake CLI) to
    stderr. A ``NullHandler`` (``--no-logging``) and the configured level are
    left untouched.
    """
    import sys

    from rich.console import Console
    from rich.logging import RichHandler

    for handler in logging.getLogger().handlers:
        if isinstance(handler, RichHandler):
            if not handler.console.stderr:
                handler.console = Console(stderr=True)
        elif isinstance(handler, logging.StreamHandler):
            if handler.stream is sys.stdout:
                handler.stream = sys.stderr


async def run_stdio(
    json_file: Optional[Path] = None,
    local_config_path: Optional[Path] = None,
) -> None:
    """Run the MCP server over the stdio transport."""
    global compiler
    _redirect_logging_off_stdout()
    tools.load_plugin_tools()
    compiler = _init_compiler(json_file, local_config_path)
    compile_task = asyncio.create_task(compiler.run())

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        compile_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await compile_task


async def run_http(
    host: str,
    port: int,
    json_file: Optional[Path] = None,
    local_config_path: Optional[Path] = None,
) -> None:
    """Run the MCP server over the streamable HTTP transport."""
    global compiler

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    tools.load_plugin_tools()
    compiler = _init_compiler(json_file, local_config_path)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=True,
    )

    class McpEndpoint:
        async def __call__(self, scope, receive, send):
            await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        compile_task = asyncio.create_task(compiler.run())
        try:
            async with session_manager.run():
                yield
        finally:
            compile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await compile_task

    async def healthz(request):
        return JSONResponse({"status": "ok"})

    async def readyz(request):
        if compiler.is_ready():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "compiling"}, status_code=503)

    starlette_app = Starlette(
        routes=[
            Route("/mcp", endpoint=McpEndpoint()),
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/readyz", endpoint=readyz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )

    config = uvicorn.Config(starlette_app, host=host, port=port)
    uv_server = uvicorn.Server(config)
    await uv_server.serve()
