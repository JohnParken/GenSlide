"""Goal and milestone ledger for cloud long-horizon agent tasks.

Maintains structured progress tracking across multi-step agent workflows
to prevent goal drift and attention loss.
"""
from __future__ import annotations

from enum import Enum
import uuid
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(str, Enum):
    """Lifecycle status of a single milestone task."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class TaskItem(BaseModel):
    """Single milestone step within a long-horizon mission."""
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    result_summary: str = Field(default="", max_length=2000)


class GoalLedger(BaseModel):
    """Ledger tracking overall mission objective and individual milestone tasks."""
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(default="", max_length=2000)
    tasks: list[TaskItem] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)

    @property
    def active_task(self) -> TaskItem | None:
        """Return the currently in-progress task, if any."""
        for t in self.tasks:
            if t.status == TaskStatus.IN_PROGRESS:
                return t
        return None

    def add_task(self, title: str, description: str = "") -> TaskItem:
        """Add a new task to the ledger."""
        item = TaskItem(title=title, description=description, status=TaskStatus.PENDING)
        self.tasks.append(item)
        self.version += 1
        return item

    def start_task(self, task_id: str) -> TaskItem:
        """Mark a task as in_progress."""
        for t in self.tasks:
            if t.task_id == task_id:
                t.status = TaskStatus.IN_PROGRESS
                self.version += 1
                return t
        raise KeyError(f"Task with id {task_id} not found in ledger")

    def complete_task(self, task_id: str, result_summary: str = "") -> TaskItem:
        """Mark a task as completed."""
        for t in self.tasks:
            if t.task_id == task_id:
                t.status = TaskStatus.COMPLETED
                t.result_summary = result_summary
                self.version += 1
                return t
        raise KeyError(f"Task with id {task_id} not found in ledger")

    def block_task(self, task_id: str, reason: str = "") -> TaskItem:
        """Mark a task as blocked."""
        for t in self.tasks:
            if t.task_id == task_id:
                t.status = TaskStatus.BLOCKED
                t.result_summary = reason
                self.version += 1
                return t
        raise KeyError(f"Task with id {task_id} not found in ledger")

    def is_all_completed(self) -> bool:
        """Return True if tasks exist and all are completed."""
        if not self.tasks:
            return False
        return all(t.status == TaskStatus.COMPLETED for t in self.tasks)

    def progress_ratio(self) -> float:
        """Return task completion progress from 0.0 to 1.0."""
        if not self.tasks:
            return 0.0
        completed_count = sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED)
        return completed_count / len(self.tasks)

    def to_milestones_tuple(self) -> tuple[dict[str, Any], ...]:
        """Format tasks for direct compatibility with PromptContext.milestones."""
        return tuple(
            {
                "task_id": t.task_id,
                "title": t.title,
                "description": t.description,
                "status": t.status.value,
                "result_summary": t.result_summary,
            }
            for t in self.tasks
        )
