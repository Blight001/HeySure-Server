# device runtime tool: fs.write
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import os
p = os.path.join(os.getcwd(), str(args['path']))
os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
with open(p, 'w', encoding='utf-8') as f:
    f.write(str(args.get('content', '')))
result = {'ok': True, 'path': p}
