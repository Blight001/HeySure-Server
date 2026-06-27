# device runtime tool: speech.speak
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import pyttsx3
engine = pyttsx3.init()
if args.get('rate') is not None:
    engine.setProperty('rate', int(args['rate']))
if args.get('volume') is not None:
    engine.setProperty('volume', float(args['volume']) / 100.0)
engine.say(str(args.get('text', '')))
engine.runAndWait()
result = {'ok': True}
