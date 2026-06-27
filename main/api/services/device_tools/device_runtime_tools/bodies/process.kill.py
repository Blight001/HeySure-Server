# device runtime tool: process.kill
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import psutil
p = psutil.Process(int(args['pid']))
p.terminate()
result = {'ok': True, 'pid': int(args['pid'])}
