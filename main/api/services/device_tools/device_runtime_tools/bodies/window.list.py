# device runtime tool: window.list
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import sys
wins = []
if sys.platform == 'win32':
    import pygetwindow as gw
    wins = [t for t in gw.getAllTitles() if t and t.strip()]
else:
    import subprocess
    out = subprocess.run(['wmctrl', '-l'], capture_output=True, text=True).stdout
    wins = [' '.join(line.split()[3:]) for line in out.splitlines() if line.strip()]
result = {'windows': wins}
