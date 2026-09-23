"""Render tool payloads as LLM-friendly TOON.

The model reads compact TOON (columns declared once) as the tool's text content,
while the full structured dict stays attached for programmatic clients. Applied
to the array/tabular tools where TOON pays off; schema- and action-shaped tools
keep their plain structured output.
"""
from __future__ import annotations

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from lobstr_mcp.toon import to_toon


def toon_result(data: dict) -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=to_toon(data))],
        structured_content=data,
    )
