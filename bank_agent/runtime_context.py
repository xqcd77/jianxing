"""Trusted service identity shared across threads, workflows and model calls."""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskContext:
    owner_id: str = ""
    conversation_id: str = ""
    task_id: str = ""
    request_id: str = ""
    model_id: str = ""
    model_name: str = ""
    model_context_tokens: int | None = None
    model_explicit: bool = False
    model_credential_version: int | None = None


task_state = ContextVar("bank_agent_task_context", default=TaskContext())
