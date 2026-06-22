# device runtime tool: ui.inspect
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import uiautomation as auto
title = args.get('title')
root = auto.WindowControl(searchDepth=2, SubName=str(title)) if title else auto.GetForegroundControl()
limit = int(args.get('max') or 150)
depth = int(args.get('max_depth') or 8)
elems = []
for c, d in auto.WalkControl(root, maxDepth=depth):
    r = c.BoundingRectangle
    elems.append({'name': c.Name, 'control_type': c.ControlTypeName, 'automation_id': c.AutomationId, 'rect': [r.left, r.top, r.right, r.bottom]})
    if len(elems) >= limit:
        break
result = {'window': root.Name, 'elements': elems}
