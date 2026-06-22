# device runtime tool: vision.capture_mouse
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import mss, base64, io, pyautogui
from PIL import Image
r = int(args.get('radius') or 200)
mx, my = pyautogui.position()
box = {'left': max(0, mx - r), 'top': max(0, my - r), 'width': r * 2, 'height': r * 2}
with mss.mss() as sct:
    raw = sct.grab(box)
img = Image.frombytes('RGB', raw.size, raw.rgb)
buf = io.BytesIO(); img.save(buf, format='JPEG', quality=70)
result = {'dataUrl': 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode(), 'width': img.width, 'height': img.height, 'send_to_user': args.get('send_to_user', True)}
