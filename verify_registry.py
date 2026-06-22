import sys, os
sys.path.insert(0,'main'); sys.path.insert(0,'.')
from mcp_runtime.mcp import registry
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
tools = [t.get('name') for t in registry.list_tools()]
print('Registered tools count:', len(tools))
print('Has mcp.describe_tool?', 'mcp.describe_tool' in tools)
print('MCP_INTROSPECTION_TOOLS =', MCP_INTROSPECTION_TOOLS)
assert 'mcp.describe_tool' not in tools, 'describe tool should be removed'
assert MCP_INTROSPECTION_TOOLS == set() or len(MCP_INTROSPECTION_TOOLS) == 0
print('VERIFIED: describe_tool removed, project imports and registry work.')
