# device runtime tool: process.list
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import psutil
needle = str(args.get('name_contains') or '').lower()
rows = []
for p in psutil.process_iter(['pid', 'name']):
    name = p.info.get('name') or ''
    if needle and needle not in name.lower():
        continue
    rows.append({'pid': p.info['pid'], 'name': name})
result = {'processes': rows[:500], 'count': len(rows)}
