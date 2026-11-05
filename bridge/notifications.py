from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from .task_service import BridgeService, BridgeTask


class TaskNotificationManager:
    """Delivers persisted terminal notifications independently of task state."""

    def __init__(
        self,
        *,
        service: BridgeService,
        send_call: Callable[[str, str], Awaitable[bool]],
        poll_seconds: int = 5,
        max_attempts: int = 3,
        retry_delay_seconds: int = 30,
    ) -> None:
        self.service = service
        self.send_call = send_call
        self.poll_seconds = max(1, int(poll_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay_seconds = max(1, int(retry_delay_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> int:
        sent = 0
        for task in self.service.notification_candidates(limit=10):
            try:
                success = bool(await self.send_call(task.qq_session_id, self._message(task)))
                if not success:
                    raise RuntimeError("AstrBot did not find a matching session")
            except Exception as exc:
                self.service.finish_notification(
                    task.task_id,
                    success=False,
                    error=str(exc),
                    max_attempts=self.max_attempts,
                    retry_delay_seconds=self.retry_delay_seconds,
                )
            else:
                self.service.finish_notification(
                    task.task_id,
                    success=True,
                    max_attempts=self.max_attempts,
                    retry_delay_seconds=self.retry_delay_seconds,
                )
                sent += 1
        return sent

    async def _loop(self) -> None:
        try:
            while not self._stopping:
                await self.run_once()
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _message(task: BridgeTask) -> str:
        labels = {
            "completed": "已完成",
            "failed": "执行失败",
            "cancelled": "已取消",
            "timed_out": "已超时",
            "unknown": "状态无法确认，需要人工检查",
        }
        lines = [
            f"Feiyu 异步任务 {labels.get(task.state.value, task.state.value)}",
            f"任务 ID：{task.task_id}",
        ]
        if task.state.value == "completed" and task.result is not None:
            if isinstance(task.result, dict) and isinstance(task.result.get("value"), str):
                summary = task.result["value"]
            else:
                summary = json.dumps(task.result, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"结果：{summary[:1500]}")
        elif task.error:
            lines.append(f"说明：{str(task.error.get('message') or task.error.get('code') or '')[:500]}")
        return "\n".join(lines)
