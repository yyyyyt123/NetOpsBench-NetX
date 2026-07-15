"""FastMCP server exposing NetOpsBench toolkit tools."""

from .mcp.registry import group_tool_names, load_tool_specs

_TOOL_SPECS = load_tool_specs()
for _spec in _TOOL_SPECS:
    globals()[_spec.name] = _spec.handler

EXPOSED_TOOLS = [_spec.name for _spec in _TOOL_SPECS]
EXPOSED_TOOLS_BY_GROUP = group_tool_names(_TOOL_SPECS)


def create_server():
    """Create the optional FastMCP server only when it is actually used."""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "fastmcp is not installed. Install with: pip install -e '.[agent]' or pip install -e '.[dev,agent]'"
        ) from exc
    server = FastMCP("netopsbench")
    for spec in _TOOL_SPECS:
        server.tool(name=spec.name)(spec.handler)
    return server


def run_server():
    """Entry point used by CLI command."""
    create_server().run(transport="stdio")


def main() -> int:
    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
