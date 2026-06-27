# device runtime tool: screen.capture
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import mss, base64, io
from PIL import Image
disp = int(args.get('display') or 0)
with mss.mss() as sct:
    mons = sct.monitors
    mon = mons[disp + 1] if 0 <= disp + 1 < len(mons) else mons[0]
    raw = sct.grab(mon)
img = Image.frombytes('RGB', raw.size, raw.rgb)
if img.width > 1280:
    img = img.resize((1280, max(1, img.height * 1280 // img.width)))
buf = io.BytesIO(); img.save(buf, format='JPEG', quality=60)
result = {'dataUrl': 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode(), 'width': img.width, 'height': img.height}
