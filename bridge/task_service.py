from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .protocol import (
    AUTONOMOUS_DIARY_APPEND,
    AUTONOMOUS_DIARY_READ,
    BRIDGE_CAPABILITIES,
    canonical_json,
    validate_capability,
    validate_capability_arguments,
    validate_capability_result,
    validate_idempotency_key,
)
from .store import TaskStore


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    LEASED = "running"  # source compatibility with v1 tests/callers
    COMPLETED = "completed"
    SUCCEEDED = "completed"  # source compatibility with v1
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.TIMED_OUT,
    TaskState.UNKNOWN,
}
NOTIFIABLE_STATES = TERMINAL_STATES


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class BridgeTask:
    task_id: str
    capability: str
    arguments: dict[str, Any]
    origin: str
    trusted_qq_id: str
    qq_session_id: str
    idempotency_key: str
    request_digest: str
    created_at: float
    deadline_at: float
    state: TaskState = TaskState.QUEUED
    started_at: float | None = None
    ended_at: float | None = None
    client_id: str | None = None
    dsh_session_id: str | None = None
    execution_id: str | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    result_digest: str | None = None
    notify_on_terminal: bool = False
    notification_status: str = "not_required"
    recovery_pending: bool = False
    future: asyncio.Future[Any] | None = field(default=None, repr=False)

    @property
    def leased_at(self) -> float | None:
        return self.started_at

    @leased_at.setter
    def leased_at(self, value: float | None) -> None:
        self.started_at = value

    @property
    def completed_at(self) -> float | None:
        return self.ended_at

    @completed_at.setter
    def completed_at(self, value: float | None) -> None:
        self.ended_at = value

    def public(
        self,
        *,
        include_arguments: bool = False,
        include_result: bool = True,
        wire: bool = False,
    ) -> dict[str, Any]:
        if wire and self.state == TaskState.RUNNING:
            state = "leased"
        elif wire and self.state == TaskState.COMPLETED:
            state = "succeeded"
        else:
            state = self.state.value
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "capability": self.capability,
            "state": state,
            "status": self.state.value,
            "created_at": int(self.created_at),
            "started_at": None if self.started_at is None else int(self.started_at),
            "ended_at": None if self.ended_at is None else int(self.ended_at),
            "deadline_at": int(self.deadline_at),
            "dsh_session_id": self.dsh_session_id,
            "execution_id": self.execution_id,
            "notification_status": self.notification_status,
        }
        if include_arguments:
            payload["arguments"] = self.arguments
        if include_result and self.result is not None:
            payload["result"] = self.result
        if self.error:
            payload["error"] = self.error
        return payload


class BridgeService:
    def __init__(
        self,
        *,
        client_id: str,
        allowed_capabilities: set[str],
        heartbeat_timeout_seconds: int = 25,
        max_queue_size: int = 8,
        max_payload_bytes: int = 262_144,
        task_retention_seconds: int = 300,
        database_path: str | Path = ":memory:",
        recovery_grace_seconds: int = 30,
    ) -> None:
        self.client_id = client_id
        validated = {validate_capability(item) for item in allowed_capabilities}
        unsupported = validated - BRIDGE_CAPABILITIES
        if unsupported:
            raise ValueError(f"unsupported Bridge capability: {sorted(unsupported)[0]}")
        self.allowed_capabilities = validated
        self.heartbeat_timeout_seconds = max(10, int(heartbeat_timeout_seconds))
        self.max_queue_size = max(1, int(max_queue_size))
        self.max_payload_bytes = max(4096, int(max_payload_bytes))
        self.task_retention_seconds = max(30, int(task_retention_seconds))
        self.recovery_grace_seconds = max(5, int(recovery_grace_seconds))

        self.store = TaskStore(database_path)
        self._tasks: dict[str, BridgeTask] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._queue: deque[str] = deque()
        self._condition = asyncio.Condition()
        self._active_task_id: str | None = None
        self._last_heartbeat: float | None = None
        self._client_capabilities: set[str] = set()
        self._agent_version = ""
        self._shutting_down = False
        self._started_at = time.time()
        self._restore()

    def _restore(self) -> None:
        active: list[BridgeTask] = []
        for row in self.store.all():
            task = self._from_row(row)
            self._tasks[task.task_id] = task
            self._idempotency[(task.origin, task.idempotency_key)] = task.task_id
            if task.state == TaskState.QUEUED:
                self._queue.append(task.task_id)
            elif task.state in {
                TaskState.RUNNING,
                TaskState.CANCEL_REQUESTED,
                TaskState.TIMED_OUT,
            }:
                task.recovery_pending = True
                self.store.update(task.task_id, recovery_pending=1)
                active.append(task)
        if active:
            active.sort(key=lambda item: item.started_at or item.created_at)
            self._active_task_id = active[0].task_id
            for duplicate in active[1:]:
                self._mark_unknown(duplicate, "multiple persisted active tasks")

    def _from_row(self, row: Any) -> BridgeTask:
        return BridgeTask(
            task_id=row["task_id"],
            capability=row["capability"],
            arguments=self.store.loads(row["arguments_json"]),
            origin=row["origin"],
            trusted_qq_id=row["trusted_qq_id"],
            qq_session_id=row["qq_session_id"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            created_at=row["created_at"],
            deadline_at=row["deadline_at"],
            state=TaskState(row["status"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            client_id=row["client_id"],
            dsh_session_id=row["dsh_session_id"],
            execution_id=row["execution_id"],
            result=self.store.loads(row["result_json"]),
            error=self.store.loads(row["error_json"]),
            result_digest=row["result_digest"],
            notify_on_terminal=bool(row["notify_on_terminal"]),
            notification_status=row["notification_status"],
            recovery_pending=bool(row["recovery_pending"]),
        )

    def _persist_new(self, task: BridgeTask) -> None:
        now = time.time()
        self.store.insert(
            {
                "task_id": task.task_id,
                "capability": task.capability,
                "arguments_json": self.store.dumps(task.arguments),
                "task_content": str(task.arguments.get("prompt") or ""),
                "origin": task.origin,
                "trusted_qq_id": task.trusted_qq_id,
                "qq_session_id": task.qq_session_id,
                "idempotency_key": task.idempotency_key,
                "request_digest": task.request_digest,
                "created_at": task.created_at,
                "deadline_at": task.deadline_at,
                "status": task.state.value,
                "notify_on_terminal": int(task.notify_on_terminal),
                "notification_status": task.notification_status,
                "recovery_pending": 0,
                "updated_at": now,
            }
        )

    def _persist_task(self, task: BridgeTask) -> None:
        self.store.update(
            task.task_id,
            status=task.state.value,
            started_at=task.started_at,
            ended_at=task.ended_at,
            deadline_at=task.deadline_at,
            client_id=task.client_id,
            dsh_session_id=task.dsh_session_id,
            execution_id=task.execution_id,
            result_json=None if task.result is None else self.store.dumps(task.result),
            error_json=None if task.error is None else self.store.dumps(task.error),
            result_digest=task.result_digest,
            notification_status=task.notification_status,
            recovery_pending=int(task.recovery_pending),
        )

    def body_status(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else now
        age = None if self._last_heartbeat is None else max(0.0, current - self._last_heartbeat)
        online = not self._shutting_down and age is not None and age <= self.heartbeat_timeout_seconds
        active = self._tasks.get(self._active_task_id or "")
        return {
            "online": online,
            "state": "online" if online else "offline",
            "last_heartbeat_age_seconds": None if age is None else round(age, 3),
            "capabilities": sorted(self.allowed_capabilities & self._client_capabilities),
            "agent_version": self._agent_version or None,
            "active_task": active.public(include_result=False) if active else None,
            "queued_tasks": sum(
                1 for task_id in self._queue
                if self._tasks.get(task_id) and self._tasks[task_id].state == TaskState.QUEUED
            ),
        }

    async def heartbeat(
        self,
        *,
        client_id: str,
        capabilities: list[str],
        agent_version: str = "",
        active_task_id: str | None = None,
        pending_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if client_id != self.client_id:
            raise BridgeError("AUTH_CLIENT_REJECTED", "client id rejected")
        self._expire_deadlines()
        accepted: set[str] = set()
        for item in capabilities:
            try:
                capability = validate_capability(item)
            except ValueError:
                continue
            if capability in self.allowed_capabilities:
                accepted.add(capability)
        self._last_heartbeat = time.time()
        self._client_capabilities = accepted
        self._agent_version = str(agent_version or "")[:64]

        pending_ids = set(pending_task_ids or [])
        active = self._tasks.get(self._active_task_id or "")
        if active and active.recovery_pending:
            if active_task_id == active.task_id:
                active.recovery_pending = False
                self._persist_task(active)
            elif active.task_id in pending_ids:
                # DSH completed locally while Bridge was unavailable and still
                # has the durable-to-process outcome queued for delivery.
                active.recovery_pending = False
                self._persist_task(active)
            else:
                self._mark_unknown(active, "DSH heartbeat did not confirm persisted execution")
                self._active_task_id = None
                active = None
        elif active_task_id and not active:
            candidate = self._tasks.get(active_task_id)
            if candidate and candidate.state in {TaskState.RUNNING, TaskState.CANCEL_REQUESTED, TaskState.TIMED_OUT}:
                candidate.recovery_pending = False
                self._active_task_id = candidate.task_id
                self._persist_task(candidate)
                active = candidate

        cancel_requested = bool(
            active_task_id
            and active
            and active.task_id == active_task_id
            and active.state in {TaskState.CANCEL_REQUESTED, TaskState.TIMED_OUT}
        )
        async with self._condition:
            self._condition.notify_all()
        return {**self.body_status(), "cancel_requested": cancel_requested, "server_time": int(time.time())}

    async def create_task(
        self,
        *,
        capability: str,
        arguments: dict[str, Any],
        origin: str,
        idempotency_key: str,
        timeout_seconds: int,
        trusted_qq_id: str = "",
        qq_session_id: str | None = None,
        notify_on_terminal: bool = False,
        require_online: bool = True,
    ) -> BridgeTask:
        if self._shutting_down:
            raise BridgeError("BRIDGE_SHUTTING_DOWN", "bridge is shutting down")
        capability = validate_capability(capability)
        idempotency_key = validate_idempotency_key(idempotency_key)
        validate_capability_arguments(capability, arguments)
        if capability not in self.allowed_capabilities:
            raise BridgeError("CAPABILITY_DENIED", "capability is not allowlisted")
        if len(canonical_json(arguments)) > self.max_payload_bytes:
            raise BridgeError("PAYLOAD_TOO_LARGE", "task arguments exceed size limit")
        if require_online and not self.body_status()["online"]:
            raise BridgeError("BODY_OFFLINE", "Windows DSH Agent is offline", retryable=True)
        if require_online and capability not in self._client_capabilities:
            raise BridgeError("CAPABILITY_UNAVAILABLE", "Windows DSH Agent did not advertise this capability", retryable=True)

        digest = hashlib.sha256(canonical_json({"capability": capability, "arguments": arguments})).hexdigest()
        existing_id = self._idempotency.get((origin, idempotency_key))
        if existing_id:
            existing = self._tasks.get(existing_id)
            if existing is None:
                row = self.store.get(existing_id)
                existing = self._from_row(row) if row else None
                if existing:
                    self._tasks[existing.task_id] = existing
            if existing:
                if existing.request_digest != digest:
                    raise BridgeError("IDEMPOTENCY_CONFLICT", "idempotency key was already used for a different request")
                return existing

        queued = sum(1 for task in self._tasks.values() if task.state == TaskState.QUEUED)
        if queued >= self.max_queue_size:
            raise BridgeError("QUEUE_FULL", "bridge task queue is full", retryable=True)
        now = time.time()
        task = BridgeTask(
            task_id=str(uuid.uuid4()),
            capability=capability,
            arguments=arguments,
            origin=origin,
            trusted_qq_id=str(trusted_qq_id),
            qq_session_id=str(qq_session_id or origin),
            idempotency_key=idempotency_key,
            request_digest=digest,
            created_at=now,
            deadline_at=now + max(1, int(timeout_seconds)),
            notify_on_terminal=bool(notify_on_terminal),
            notification_status="waiting" if notify_on_terminal else "not_required",
            future=asyncio.get_running_loop().create_future(),
        )
        self._persist_new(task)
        self._tasks[task.task_id] = task
        self._idempotency[(origin, idempotency_key)] = task.task_id
        self._queue.append(task.task_id)
        async with self._condition:
            self._condition.notify_all()
        return task

    async def wait_for_result(self, task: BridgeTask) -> Any:
        if task.state == TaskState.COMPLETED:
            return task.result
        if task.state in TERMINAL_STATES:
            self._raise_terminal(task)
        if task.future is None:
            task.future = asyncio.get_running_loop().create_future()
        remaining = max(0.0, task.deadline_at - time.time())
        try:
            return await asyncio.wait_for(asyncio.shield(task.future), remaining)
        except TimeoutError as exc:
            self._expire_deadlines()
            raise BridgeError("TASK_TIMEOUT", "DSH task timed out", retryable=True) from exc

    async def lease_task(self, *, client_id: str, wait_seconds: float = 20) -> dict[str, Any] | None:
        if client_id != self.client_id:
            raise BridgeError("AUTH_CLIENT_REJECTED", "client id rejected")
        wait_seconds = min(max(float(wait_seconds), 0.0), 25.0)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        async with self._condition:
            while not self._shutting_down:
                self._expire_deadlines()
                if self._active_task_id is None:
                    while self._queue:
                        task = self._tasks.get(self._queue.popleft())
                        if not task or task.state != TaskState.QUEUED:
                            continue
                        task.state = TaskState.RUNNING
                        task.started_at = time.time()
                        task.client_id = client_id
                        task.recovery_pending = False
                        self._active_task_id = task.task_id
                        self._persist_task(task)
                        return task.public(include_arguments=True, include_result=False, wire=True)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except TimeoutError:
                    return None
        return None

    async def submit_result(
        self,
        *,
        client_id: str,
        task_id: str,
        status: str,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> BridgeTask:
        self._expire_deadlines()
        task = self._require_task(task_id)
        if client_id != self.client_id or task.client_id not in {None, client_id}:
            raise BridgeError("AUTH_CLIENT_REJECTED", "client id rejected")
        if status not in {"succeeded", "failed"}:
            raise BridgeError("INVALID_RESULT", "status must be succeeded or failed")
        if status == "succeeded":
            validate_capability_result(task.capability, result)
            if task.capability == AUTONOMOUS_DIARY_APPEND and result["entry_id"] != task.arguments["entry_id"]:
                raise ValueError("result entry_id does not match the request")
            if task.capability == AUTONOMOUS_DIARY_READ and len(result["entries"]) > task.arguments.get("limit", 1):
                raise ValueError("result entries exceed the requested limit")
        encoded = canonical_json({"status": status, "result": result, "error": error})
        if len(encoded) > self.max_payload_bytes:
            raise BridgeError("PAYLOAD_TOO_LARGE", "task result exceeds size limit")
        digest = hashlib.sha256(encoded).hexdigest()
        if task.state in TERMINAL_STATES or task.state == TaskState.CANCEL_REQUESTED:
            if task.result_digest == digest:
                return task
            raise BridgeError("TASK_ALREADY_TERMINAL", "task already has a terminal result")
        if task.state != TaskState.RUNNING:
            raise BridgeError("TASK_NOT_LEASED", "task is not leased")

        task.ended_at = time.time()
        task.result_digest = digest
        if isinstance(result, dict):
            task.dsh_session_id = self._optional_id(result.get("dsh_session_id"))
            task.execution_id = self._optional_id(result.get("execution_id"))
        if status == "succeeded":
            task.state = TaskState.COMPLETED
            task.result = result
            if task.future is not None and not task.future.done():
                task.future.set_result(result)
        else:
            normalized = error if isinstance(error, dict) else {}
            task.state = TaskState.FAILED
            task.error = {
                "code": str(normalized.get("code") or "AGENT_TASK_FAILED")[:64],
                "message": str(normalized.get("message") or "DSH task failed")[:512],
                "retryable": bool(normalized.get("retryable", False)),
            }
            self._set_future_error(task, BridgeError(task.error["code"], task.error["message"], retryable=task.error["retryable"]))
        self._terminal_notification(task)
        self._persist_task(task)
        if self._active_task_id == task.task_id:
            self._active_task_id = None
        async with self._condition:
            self._condition.notify_all()
        return task

    async def cancel_task(self, task_id: str, *, timed_out: bool = False) -> BridgeTask:
        self._expire_deadlines()
        task = self._require_task(task_id)
        if task.state == TaskState.UNKNOWN:
            raise BridgeError(
                "CANCEL_UNAVAILABLE",
                "DSH execution state is unknown; cancellation cannot be confirmed",
            )
        if task.state in TERMINAL_STATES:
            return task
        if task.state == TaskState.QUEUED:
            task.state = TaskState.TIMED_OUT if timed_out else TaskState.CANCELLED
            task.ended_at = time.time()
            self._set_future_error(task, self._cancel_error(timed_out))
            self._terminal_notification(task)
        elif task.state == TaskState.RUNNING:
            task.state = TaskState.TIMED_OUT if timed_out else TaskState.CANCEL_REQUESTED
            if timed_out:
                task.ended_at = time.time()
                self._terminal_notification(task)
            self._set_future_error(task, self._cancel_error(timed_out))
        elif task.state == TaskState.CANCEL_REQUESTED:
            return task
        else:
            raise BridgeError("CANCEL_UNAVAILABLE", "task execution cannot be reliably cancelled")
        self._persist_task(task)
        async with self._condition:
            self._condition.notify_all()
        return task

    async def acknowledge_cancellation(self, *, client_id: str, task_id: str) -> BridgeTask:
        self._expire_deadlines()
        task = self._require_task(task_id)
        if client_id != self.client_id or task.client_id not in {None, client_id}:
            raise BridgeError("AUTH_CLIENT_REJECTED", "client id rejected")
        if task.state == TaskState.CANCEL_REQUESTED:
            task.state = TaskState.CANCELLED
            task.ended_at = time.time()
            self._terminal_notification(task)
        elif task.state not in {TaskState.CANCELLED, TaskState.TIMED_OUT}:
            raise BridgeError("TASK_NOT_CANCELLED", "task is not cancelled")
        task.recovery_pending = False
        self._persist_task(task)
        if self._active_task_id == task.task_id:
            self._active_task_id = None
        async with self._condition:
            self._condition.notify_all()
        return task

    def task_status(
        self, task_id: str, *, owner: str | None = None, wire: bool = False
    ) -> dict[str, Any]:
        self._expire_deadlines()
        task = self._require_task(task_id)
        if owner is not None and task.trusted_qq_id != owner:
            raise BridgeError("TASK_NOT_FOUND", "task not found")
        return task.public(wire=wire)

    def list_tasks(
        self,
        *,
        owner: str,
        status: str | None = None,
        created_after: float | None = None,
        created_before: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if status is not None:
            try:
                status = TaskState(status).value
            except ValueError as exc:
                raise BridgeError("INVALID_REQUEST", "invalid task status") from exc
        return [
            self._from_row(row).public(include_result=False)
            for row in self.store.list_for_owner(
                owner,
                status=status,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
            )
        ]

    def notification_candidates(self, *, limit: int = 10) -> list[BridgeTask]:
        return [self._from_row(row) for row in self.store.claim_notifications(now=time.time(), limit=limit) if row]

    def finish_notification(
        self,
        task_id: str,
        *,
        success: bool,
        error: str = "",
        max_attempts: int = 3,
        retry_delay_seconds: int = 30,
    ) -> None:
        row = self.store.get(task_id)
        if not row:
            return
        attempts = int(row["notification_attempts"]) + 1
        if success:
            self.store.update(
                task_id,
                notification_status="sent",
                notification_attempts=attempts,
                notification_last_error=None,
                notification_next_at=None,
                notified_at=time.time(),
            )
        else:
            final = attempts >= max(1, max_attempts)
            self.store.update(
                task_id,
                notification_status="failed" if final else "retry",
                notification_attempts=attempts,
                notification_last_error=str(error)[:512],
                notification_next_at=None if final else time.time() + max(1, retry_delay_seconds) * attempts,
            )
        task = self._tasks.get(task_id)
        if task:
            task.notification_status = "sent" if success else ("failed" if attempts >= max_attempts else "retry")

    async def shutdown(self) -> None:
        # Preserve queued/running records for restart recovery. The DSH side will
        # reconnect and either confirm its active task or cause it to become unknown.
        self._shutting_down = True
        async with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        self.store.close()

    def cleanup(self) -> None:
        self._expire_deadlines()
        active = self._tasks.get(self._active_task_id or "")
        if (
            active
            and active.recovery_pending
            and time.time() - self._started_at >= self.recovery_grace_seconds
        ):
            self._mark_unknown(active, "DSH did not reconnect during recovery window")
            self._active_task_id = None

    def _expire_deadlines(self) -> None:
        now = time.time()
        for task in list(self._tasks.values()):
            if task.state in {TaskState.QUEUED, TaskState.RUNNING} and task.deadline_at <= now:
                task.state = TaskState.TIMED_OUT
                task.ended_at = now
                self._set_future_error(task, self._cancel_error(True))
                self._terminal_notification(task)
                self._persist_task(task)

    def _mark_unknown(self, task: BridgeTask, message: str) -> None:
        task.state = TaskState.UNKNOWN
        task.ended_at = time.time()
        task.recovery_pending = False
        task.error = {"code": "EXECUTION_STATE_UNKNOWN", "message": message[:512], "retryable": False}
        self._set_future_error(task, BridgeError(task.error["code"], task.error["message"]))
        self._terminal_notification(task)
        self._persist_task(task)

    def _terminal_notification(self, task: BridgeTask) -> None:
        if task.notify_on_terminal and task.state in NOTIFIABLE_STATES and task.notification_status not in {"sent", "failed"}:
            task.notification_status = "pending"
            self.store.update(task.task_id, notification_status="pending", notification_next_at=time.time())

    def _require_task(self, task_id: str) -> BridgeTask:
        try:
            normalized = str(uuid.UUID(str(task_id)))
        except ValueError as exc:
            raise BridgeError("INVALID_TASK_ID", "invalid task id") from exc
        task = self._tasks.get(normalized)
        if not task:
            row = self.store.get(normalized)
            if row:
                task = self._from_row(row)
                self._tasks[normalized] = task
        if not task:
            raise BridgeError("TASK_NOT_FOUND", "task not found")
        return task

    @staticmethod
    def _optional_id(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()[:128]

    @staticmethod
    def _cancel_error(timed_out: bool) -> BridgeError:
        return BridgeError(
            "TASK_TIMEOUT" if timed_out else "TASK_CANCELLED",
            "task timed out" if timed_out else "task cancellation was requested",
            retryable=timed_out,
        )

    @staticmethod
    def _set_future_error(task: BridgeTask, exc: BridgeError) -> None:
        if task.future is not None and not task.future.done():
            task.future.set_exception(exc)
            task.future.exception()

    @staticmethod
    def _raise_terminal(task: BridgeTask) -> None:
        error = task.error or {}
        raise BridgeError(
            str(error.get("code") or f"TASK_{task.state.value.upper()}"),
            str(error.get("message") or f"task is {task.state.value}"),
            retryable=bool(error.get("retryable", False)),
        )
