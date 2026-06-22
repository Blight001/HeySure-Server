# -*- coding: utf-8 -*-
"""Built-in library (知识工坊) MCP tool catalog.

Knowledge-base operations are exposed exclusively via the registry tool
``knowledge.manage`` (action dispatch). This catalog is intentionally empty
so the library does not duplicate those capabilities.
"""

TOOL_DEFS: list = []

TOOL_NAMES = [item["name"] for item in TOOL_DEFS]