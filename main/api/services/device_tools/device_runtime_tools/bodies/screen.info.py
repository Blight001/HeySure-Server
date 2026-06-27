# device runtime tool: screen.info
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import mss
with mss.mss() as sct:
    mons = sct.monitors[1:]
result = {'monitors': [{'index': i, 'left': m['left'], 'top': m['top'], 'width': m['width'], 'height': m['height']} for i, m in enumerate(mons)]}
