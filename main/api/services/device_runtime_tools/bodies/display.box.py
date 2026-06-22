# device runtime tool: display.box
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import tkinter as tk
left = int(args.get('left') or 0)
top = int(args.get('top') or 0)
width = int(args.get('width'))
height = int(args.get('height'))
duration = int(args.get('duration') or 1000)
color = str(args.get('color') or 'red')
label = str(args.get('label') or '')
root = tk.Tk()
root.overrideredirect(True)
root.attributes('-topmost', True)
try:
    root.attributes('-alpha', 0.35)
except Exception:
    pass
root.geometry('%dx%d+%d+%d' % (width, height, left, top))
root.configure(bg=color)
if label:
    tk.Label(root, text=label, bg=color, fg='white').place(x=2, y=2)
root.after(max(1, duration), root.destroy)
root.mainloop()
result = {'ok': True}
