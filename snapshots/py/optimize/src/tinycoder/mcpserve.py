"""Run a set of tools as an MCP server on stdio, using the SDK's low-level server API."""

import json
import logging


class ToolError(Exception):
    """A failure reported to the client as an MCP tool error (`isError: true`)."""


def serve(name, tools, handlers):
    """Serve `tools` (list of dicts with name/description/input_schema) on stdio.

    `handlers` maps tool name -> callable(args: dict) returning a JSON-able dict, either a
    plain function or a coroutine function. Raising ToolError gives an `error: ` result.
    """
    import inspect

    from . import fastimport

    fastimport.shim("mcp", "mcp.server")  # skip the SDK's re-export __init__ files

    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

    tool_objs = [
        types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"])
        for t in tools
    ]
    is_async = {k: inspect.iscoroutinefunction(fn) for k, fn in handlers.items()}

    def error_result(message):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="error: " + message)], is_error=True
        )

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=tool_objs)

    async def call_tool(ctx, params):
        fn = handlers.get(params.name)
        if fn is None:
            return error_result(f"unknown tool: {params.name}")
        args = params.arguments or {}
        try:
            result = (await fn(args)) if is_async[params.name] else fn(args)
        except ToolError as exc:
            return error_result(str(exc))
        except Exception as exc:  # never let a tool bug become a protocol error
            return error_result(f"{type(exc).__name__}: {exc}")
        text = json.dumps(result, ensure_ascii=False)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server = Server(name, version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)
    server.middleware = []  # drop the no-op OpenTelemetry middleware

    async def main():
        from .fastio import open_stdout

        stdout = await open_stdout()  # thread-free writes; None keeps the SDK default
        async with stdio_server(stdout=stdout) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
        if stdout is not None:
            await stdout.aclose()  # nothing may be left unsent when main.py calls os._exit

    fastimport.gc_resume()
    anyio.run(main)


# --- small argument validators shared by both servers ---------------------------------


def req_str(args, key):
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"'{key}' is required and must be a string")
    return value


def opt_str(args, key, default):
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolError(f"'{key}' must be a string")
    return value


def opt_int(args, key, minimum):
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise ToolError(f"'{key}' must be an integer")
    if value < minimum:
        raise ToolError(f"'{key}' must be >= {minimum}")
    return value
