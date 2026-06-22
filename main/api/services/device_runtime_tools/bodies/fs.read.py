# device runtime tool: fs.read
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import os
p = os.path.join(os.getcwd(), str(args['path']))
cap = int(args.get('maxBytes') or 200000)
with open(p, 'r', encoding='utf-8', errors='replace') as f:
    data = f.read(cap + 1)
result = {'path': p, 'content': data[:cap], 'truncated': len(data) > cap}
