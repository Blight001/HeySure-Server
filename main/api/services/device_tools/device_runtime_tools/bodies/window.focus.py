# device runtime tool: window.focus
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import sys
title = str(args['title'])
if sys.platform == 'win32':
    import pygetwindow as gw
    ws = gw.getWindowsWithTitle(title)
    if not ws:
        raise ValueError('window not found: ' + title)
    ws[0].activate()
else:
    import subprocess
    subprocess.run(['wmctrl', '-a', title], check=False)
result = {'ok': True, 'title': title}
