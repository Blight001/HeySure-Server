"""Shared schema for curated AI-workspace file discovery."""

WORKSPACE_LIST_FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "可选的工作区相对目录；默认当前 AI 工作区根目录。"},
        "query": {"type": "string", "description": "可选的路径关键词过滤，避免把无关文件全部带回上下文。"},
        "include_generated": {"type": "boolean", "description": "是否包含构建产物、缓存、日志、数据库等生成物；默认 false。"},
        "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件；默认 false。敏感配置应只在用户明确要求时读取。"},
        "max_depth": {"type": "integer", "minimum": 1, "maximum": 32, "description": "递归深度，默认 8。"},
        "max_entries": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "最多返回条目数，默认 300。"},
    },
    "required": [],
}
