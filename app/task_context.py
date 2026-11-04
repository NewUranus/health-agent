"""任务执行身份只在服务端传递，供健康写入校验租约与去重。"""
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskExecution:
    task_id: str
    worker_token: str
    user_id: str


task_execution: ContextVar[TaskExecution | None] = ContextVar("task_execution", default=None)
