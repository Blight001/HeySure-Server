# device runtime tool: screen.capture_region
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import mss, base64, io
from PIL import Image
box = {'left': int(args.get('x') or 0), 'top': int(args.get('y') or 0), 'width': int(args['width']), 'height': int(args['height'])}
with mss.mss() as sct:
    raw = sct.grab(box)
img = Image.frombytes('RGB', raw.size, raw.rgb)
buf = io.BytesIO(); img.save(buf, format='JPEG', quality=70)
result = {'dataUrl': 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode(), 'width': img.width, 'height': img.height}
