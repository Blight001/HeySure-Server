# device runtime tool: keyboard.press
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import pyautogui
keys = args.get('keys')
if isinstance(keys, str):
    keys = [k.strip() for k in keys.replace('+', ' ').split() if k.strip()]
if not isinstance(keys, list) or not keys:
    raise ValueError('keys is required')
pyautogui.hotkey(*keys) if len(keys) > 1 else pyautogui.press(keys[0])
result = {'ok': True, 'keys': keys}
