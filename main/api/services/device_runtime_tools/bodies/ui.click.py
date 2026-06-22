# device runtime tool: ui.click
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import uiautomation as auto
title = args.get('title')
root = auto.WindowControl(searchDepth=2, SubName=str(title)) if title else auto.GetForegroundControl()
name = args.get('name'); aid = args.get('automation_id'); ctype = args.get('control_type')
depth = int(args.get('max_depth') or 8)
target = None
for c, d in auto.WalkControl(root, maxDepth=depth):
    if aid and c.AutomationId != aid:
        continue
    if name and str(name) not in (c.Name or ''):
        continue
    if ctype and c.ControlTypeName != ctype:
        continue
    target = c; break
if target is None:
    raise ValueError('control not found')
try:
    target.GetInvokePattern().Invoke()
except Exception:
    target.Click()
result = {'ok': True, 'name': target.Name}
