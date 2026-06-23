import time
from typing import Optional

from sqlmodel import Field, SQLModel


class Memory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    memory_id: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ai_config_id: Optional[int] = Field(default=None, index=True)
    project_id: Optional[str] = Field(default=None, index=True)
    job_id: Optional[str] = Field(default=None, index=True)
    generation: int = Field(default=1)
    kind: str = Field(default="fact", index=True)  # fact/decision/lesson/todo/risk/template
    tags: str = Field(default="")  # comma-separated
    content: str = Field(default="")
    source: str = Field(default="{}")  # JSON: {chat_message_id, file_path,...}
    confidence: float = Field(default=0.6)
    archived: bool = Field(default=False, index=True)
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time)


class KnowledgeEntry(SQLModel, table=True):
    """已弃用（KnowledgeEntry 表已从运行时移除）。

    知识条目（topics / 传承技能）的真相源是账号的 KnowledgeBase/ 目录下的 Markdown 文件。
    所有 list/read/consult/propose/archive 等操作现在完全基于文件系统 + embeddings/ 目录。

    此模型定义保留仅为历史迁移/检查脚本使用。生产中可安全 DROP TABLE knowledgeentry。

    与 Memory 表的关系：Memory 仍独立用于旧的 memory.* 工具。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    memory_id: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    title: str = Field(default="")
    triggers: str = Field(default="")  # 逗号分隔的触发关键词
    scope: str = Field(default="global", index=True)  # global / ai:<id> / project:<id>
    scope_target: Optional[str] = Field(default=None)
    file_path: str = Field(default="")  # 相对 KnowledgeBase/ 根目录
    summary: str = Field(default="")  # 检索摘要，1-2 句
    status: str = Field(default="active", index=True)  # active / archived
    confidence: float = Field(default=0.6)
    use_count: int = Field(default=0)
    last_used_at: Optional[float] = Field(default=None)
    source_job_id: Optional[str] = Field(default=None, index=True)
    source_generation: Optional[int] = Field(default=None)
    source_ai_config_id: Optional[int] = Field(default=None, index=True)
    source_message_id: Optional[int] = Field(default=None)
    librarian_ai_config_id: Optional[int] = Field(default=None, index=True)  # 由哪个图书管理员负责
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time)
