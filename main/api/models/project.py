import time
from typing import List, Optional

from sqlmodel import Field, SQLModel


class EvolutionProject(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    project_id: str = Field(index=True)
    name: str = Field(index=True)
    description: str = Field(default="")
    status: str = Field(default="running", index=True)  # running / ended
    ai_member_ids: str = Field(default="[]")  # JSON array, e.g. [1,2,3]
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class EvolutionProjectCreate(SQLModel):
    name: str
    description: Optional[str] = ""
    status: Optional[str] = "running"
    ai_member_ids: Optional[List[int]] = None


class EvolutionProjectUpdate(SQLModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    ai_member_ids: Optional[List[int]] = None
