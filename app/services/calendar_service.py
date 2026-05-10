import json
import os
import uuid
from datetime import datetime
from typing import Any, Optional

from app.config.settings import settings
from app.schemas.calendar import DayTask, DayTaskCreate, DayTaskUpdate

_TASKS_PATH = os.path.join(".", "data", "calendar", "day_tasks.json")
_service_singleton: Optional["DailyScheduleService"] = None


def get_daily_schedule_service() -> "DailyScheduleService":
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = DailyScheduleService()
    return _service_singleton


class DailyScheduleService:
    def __init__(self, path: str = _TASKS_PATH) -> None:
        self._path = path
        self._tasks: dict[str, DayTask] = {}
        self._repo = None
        if settings.database_url:
            try:
                self._repo = PostgresDayTaskRepository(settings.database_url)
                self._migrate_json_to_postgres()
            except Exception:
                self._repo = None
        if self._repo is None:
            self._load()

    def list_tasks(
        self,
        *,
        user_id: str = "local",
        date: str = "",
        month: str = "",
    ) -> list[DayTask]:
        if self._repo:
            return self._repo.list_tasks(user_id=user_id, date=date, month=month)
        tasks = [task for task in self._tasks.values() if task.user_id == user_id]
        if date:
            tasks = [task for task in tasks if task.task_date == date]
        if month:
            tasks = [task for task in tasks if task.task_date.startswith(month)]
        tasks.sort(key=lambda task: (task.task_date, task.start_time, task.created_at))
        return tasks

    def create_task(self, payload: DayTaskCreate) -> DayTask:
        task = DayTask(id=f"daytask_{uuid.uuid4().hex[:12]}", **payload.model_dump())
        if self._repo:
            return self._repo.upsert(task)
        self._tasks[task.id] = task
        self._save()
        return task

    def update_task(self, task_id: str, payload: DayTaskUpdate) -> DayTask:
        task = self.get_task(task_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(task, key, value)
        task.updated_at = datetime.now().isoformat()
        if self._repo:
            return self._repo.upsert(task)
        self._tasks[task.id] = task
        self._save()
        return task

    def delete_task(self, task_id: str) -> bool:
        if self._repo:
            return self._repo.delete(task_id)
        if task_id not in self._tasks:
            return False
        self._tasks.pop(task_id)
        self._save()
        return True

    def get_task(self, task_id: str) -> DayTask:
        if self._repo:
            return self._repo.get(task_id)
        if task_id not in self._tasks:
            raise KeyError(f"Day task not found: {task_id}")
        return self._tasks[task_id]

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
            self._tasks = {item["id"]: DayTask(**item) for item in raw}
        except FileNotFoundError:
            self._tasks = {}
        except Exception:
            self._tasks = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = [task.model_dump() for task in self._tasks.values()]
        data.sort(key=lambda item: (item.get("task_date", ""), item.get("start_time", "")))
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def _migrate_json_to_postgres(self) -> None:
        if not self._repo or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
            for item in raw:
                task = DayTask(**item)
                try:
                    self._repo.get(task.id)
                except KeyError:
                    self._repo.upsert(task)
        except Exception:
            pass


class PostgresDayTaskRepository:
    def __init__(self, database_url: str) -> None:
        import psycopg

        self._database_url = database_url
        self._psycopg = psycopg
        self.init_schema()

    def _connect(self):
        return self._psycopg.connect(self._database_url)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS day_tasks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    task_date DATE NOT NULL,
                    start_time TEXT NOT NULL DEFAULT '09:00',
                    end_time TEXT NOT NULL DEFAULT '10:00',
                    title TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    remind BOOLEAN NOT NULL DEFAULT FALSE,
                    completed BOOLEAN NOT NULL DEFAULT FALSE,
                    metadata JSONB DEFAULT '{}',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_day_tasks_user_date ON day_tasks(user_id, task_date)")

    def list_tasks(self, *, user_id: str, date: str = "", month: str = "") -> list[DayTask]:
        where = ["user_id = %(user_id)s"]
        params: dict[str, Any] = {"user_id": user_id}
        if date:
            where.append("task_date = %(date)s")
            params["date"] = date
        if month:
            where.append("to_char(task_date, 'YYYY-MM') = %(month)s")
            params["month"] = month
        sql = f"""
            SELECT id, user_id, task_date, start_time, end_time, title, notes,
                   remind, completed, metadata, created_at, updated_at
            FROM day_tasks
            WHERE {' AND '.join(where)}
            ORDER BY task_date ASC, start_time ASC, created_at ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_task(row) for row in rows]

    def upsert(self, task: DayTask) -> DayTask:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO day_tasks (
                    id, user_id, task_date, start_time, end_time, title, notes,
                    remind, completed, metadata, created_at, updated_at
                )
                VALUES (
                    %(id)s, %(user_id)s, %(task_date)s, %(start_time)s, %(end_time)s,
                    %(title)s, %(notes)s, %(remind)s, %(completed)s, %(metadata)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    task_date = EXCLUDED.task_date,
                    start_time = EXCLUDED.start_time,
                    end_time = EXCLUDED.end_time,
                    title = EXCLUDED.title,
                    notes = EXCLUDED.notes,
                    remind = EXCLUDED.remind,
                    completed = EXCLUDED.completed,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                self._params(task),
            )
        return task

    def get(self, task_id: str) -> DayTask:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, task_date, start_time, end_time, title, notes,
                       remind, completed, metadata, created_at, updated_at
                FROM day_tasks WHERE id = %s
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Day task not found: {task_id}")
        return self._row_to_task(row)

    def delete(self, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM day_tasks WHERE id = %s", (task_id,))
            return cur.rowcount > 0

    def _params(self, task: DayTask) -> dict[str, Any]:
        return {
            **task.model_dump(exclude={"metadata"}),
            "metadata": json.dumps(task.metadata, ensure_ascii=False),
        }

    def _row_to_task(self, row) -> DayTask:
        metadata = row[9] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return DayTask(
            id=row[0],
            user_id=row[1],
            task_date=row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
            start_time=row[3],
            end_time=row[4],
            title=row[5],
            notes=row[6] or "",
            remind=bool(row[7]),
            completed=bool(row[8]),
            metadata=metadata,
            created_at=row[10].isoformat() if hasattr(row[10], "isoformat") else str(row[10]),
            updated_at=row[11].isoformat() if hasattr(row[11], "isoformat") else str(row[11]),
        )
