# device runtime tool: git.diff
# 约定：dict `args` 已注入；把结果赋给 `result`。由 python-runner 在设备上执行。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 在 DB 中改写实例。
import os, subprocess
cwd = os.path.join(os.getcwd(), str(args.get('cwd') or '.'))
r = subprocess.run(['git', '-C', cwd, 'diff'], capture_output=True, text=True)
result = {'stdout': r.stdout[:100000], 'stderr': r.stderr[:4000], 'exitCode': r.returncode}
