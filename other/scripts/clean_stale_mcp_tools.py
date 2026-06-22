#!/usr/bin/env python3
"""
一键清理所有 AI 配置里残留的旧 MCP 工具名字 + 根据当前绑定状态过滤 gated 工具。

用法（在 server/ 目录执行）：
    PYTHONPATH=main:. python other/scripts/clean_stale_mcp_tools.py

清理后，旧的 admin.get_overview / prompt.read_ai / conversation.create 等名字会消失，
未绑定工具箱/图书馆的 AI 也会被正确裁剪。

清理是幂等的，可以反复运行。
"""
import sys
import os

# 确保能 import 到 server/main 下的模块
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "main"))
sys.path.insert(0, ROOT)

from sqlmodel import Session, select
from api.database import engine
from api.models import AssistantAIConfig
from tools.engine import sanitize_mcp_tools


def main():
    print("开始清理所有 AI 的 mcp_tools 中的旧名字和不该有的 gated 工具...")

    cleaned_count = 0
    with Session(engine) as session:
        cfgs = session.exec(select(AssistantAIConfig)).all()
        for cfg in cfgs:
            old = cfg.mcp_tools or "[]"
            # 使用 sanitize（会根据该 AI 当前的 toolbox / library 绑定状态清理）
            new = sanitize_mcp_tools(old, user_id=cfg.user_id, ai_config_id=cfg.id)
            if new != old:
                cfg.mcp_tools = new
                cleaned_count += 1
                print(f"  已清理 AI#{cfg.id} ({cfg.name or 'unnamed'})")
        if cleaned_count:
            session.commit()
        else:
            print("  没有需要清理的 AI（或已经干净）。")

    print(f"\n完成！共清理 {cleaned_count} 个 AI 配置。")
    print("建议：重启 gateway + ai_runtime 进程后，新对话的 prompt 会立即干净。")
    print("如果想只清理特定 AI，可以修改脚本只处理特定 id。")


if __name__ == "__main__":
    main()
