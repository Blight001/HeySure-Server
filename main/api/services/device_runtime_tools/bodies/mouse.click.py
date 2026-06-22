# device runtime tool: mouse.click
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import pyautogui
kw = {'button': args.get('button', 'left')}
if args.get('x') is not None and args.get('y') is not None:
    kw['x'] = args['x']; kw['y'] = args['y']
pyautogui.click(**kw)
result = {'ok': True}
