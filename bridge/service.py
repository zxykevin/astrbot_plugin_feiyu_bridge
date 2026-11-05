"""Stable import surface for the persistent Bridge task service."""

from .task_service import (
    BridgeError,
    BridgeService,
    BridgeTask,
    TaskState,
    TERMINAL_STATES,
)

__all__ = [
    "BridgeError",
    "BridgeService",
    "BridgeTask",
    "TaskState",
    "TERMINAL_STATES",
]
