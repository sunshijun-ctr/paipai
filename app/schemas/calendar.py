from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class DayTask(BaseModel):
    id: str
    user_id: str = "local"
    task_date: str
    start_time: str = "09:00"
    end_time: str = "10:00"
    title: str
    notes: str = ""
    remind: bool = False
    completed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class DayTaskCreate(BaseModel):
    user_id: str = "local"
    task_date: str
    start_time: str = "09:00"
    end_time: str = "10:00"
    title: str
    notes: str = ""
    remind: bool = False
    completed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class DayTaskUpdate(BaseModel):
    task_date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None
    remind: Optional[bool] = None
    completed: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None
