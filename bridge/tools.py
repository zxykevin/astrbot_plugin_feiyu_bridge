from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .protocol import canonical_json
from .service import BridgeError, BridgeService


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass
class GetBodyStatusTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    service: BridgeService = field(default=None)
    authorized_sender_ids: frozenset[str] = field(default_factory=frozenset)
    name: str = "get_body_status"
    description: str = (
        "查询肥鱼 Windows 身体是否在线、可用 DSH 能力和当前任务状态。"
        "此工具只读，不会启动电脑或执行任何系统操作。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        del kwargs
        identity, denied = _authorized_identity(context, self.authorized_sender_ids)
        if denied:
            return denied
        del identity
        return _json_result({"ok": True, **self.service.body_status()})


@dataclass
class DshExecuteTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    service: BridgeService = field(default=None)
    authorized_sender_ids: frozenset[str] = field(default_factory=frozenset)
    default_timeout_seconds: int = 30
    max_timeout_seconds: int = 120
    name: str = "dsh_execute"
    description: str = (
        "把真实且已授权的 QQ 用户请求交给 Windows DSH Agent 处理。"
        "提供明确、完整的自然语言 prompt；会话身份由服务端安全生成。"
        "不支持 shell、SSH、PowerShell、cmd 或任意系统命令。"
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "必填：交给 DSH Agent 的自然语言任务，不得包含任意命令执行接口。",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "可选：任务超时秒数，服务端会限制在配置上限内。",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        prompt: str,
        timeout_seconds: int | None = None,
    ) -> ToolExecResult:
        event = self._event_from_context(context)
        sender_id = self._event_value(event, "get_sender_id", "sender_id")
        origin = self._event_value(event, "unified_msg_origin")
        message = str(getattr(event, "message_str", "") or "").strip()

        if (
            not event
            or not sender_id
            or not origin
            or not message
            or not self._is_real_user_event(event)
        ):
            return self._denied("DIRECT_USER_REQUIRED", "只允许真实用户消息触发 DSH")
        if sender_id not in self.authorized_sender_ids:
            return self._denied("SENDER_NOT_AUTHORIZED", "当前用户无权调用 DSH")
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            return self._denied("INVALID_REQUEST", "prompt 必须是非空字符串且不能包含 NUL")

        try:
            if timeout_seconds is not None and (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not float(timeout_seconds).is_integer()
            ):
                raise ValueError("timeout_seconds 必须是整数")
            timeout = (
                self.default_timeout_seconds
                if timeout_seconds is None
                else int(timeout_seconds)
            )
        except (TypeError, ValueError) as exc:
            return self._denied("INVALID_REQUEST", str(exc))
        timeout = min(max(1, timeout), self.max_timeout_seconds)
        capability = "dsh.workflow.run"
        arguments = {
            "prompt": prompt,
            "session_key": self._derive_session_key(sender_id, origin),
            "timeout_seconds": timeout,
        }
        key = self._derive_idempotency_key(event, origin, capability, arguments)
        try:
            task = await self.service.create_task(
                capability=capability,
                arguments=arguments,
                origin=origin,
                idempotency_key=key,
                timeout_seconds=timeout,
                trusted_qq_id=sender_id,
                qq_session_id=origin,
            )
            result = await self.service.wait_for_result(task)
            return _json_result(
                {
                    "ok": True,
                    "task_id": task.task_id,
                    "capability": capability,
                    "result": result,
                }
            )
        except BridgeError as exc:
            return _json_result(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "retryable": exc.retryable,
                    },
                }
            )

    @staticmethod
    def _event_from_context(context: ContextWrapper[AstrAgentContext]):
        agent_context = getattr(context, "context", None)
        return getattr(agent_context, "event", None)

    @staticmethod
    def _event_value(event: Any, method_name: str, attribute_name: str = "") -> str:
        if event is None:
            return ""
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                value = method()
                return str(value).strip() if value is not None else ""
            except Exception:
                return ""
        value = getattr(event, attribute_name or method_name, "")
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _is_real_user_event(event: Any) -> bool:
        """Fail closed for AstrBot cron/proactive/synthetic agent events."""
        event_module = str(getattr(event.__class__, "__module__", ""))
        event_name = str(getattr(event.__class__, "__name__", ""))
        if not event_module.startswith("astrbot.core.platform.sources."):
            return False
        if event_name in {"CronMessageEvent", "ProactiveMessageEvent"}:
            return False
        platform_name = DshExecuteTool._event_value(event, "get_platform_name")
        if platform_name.lower() == "cron":
            return False
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "").strip()
        raw_message = getattr(message_obj, "raw_message", None)
        return bool(message_id and raw_message is not None)

    @staticmethod
    def _derive_session_key(sender_id: str, origin: str) -> str:
        digest = hashlib.sha256(
            b"feiyu-bridge-session-v1\n"
            + sender_id.encode("utf-8")
            + b"\n"
            + origin.encode("utf-8")
        ).hexdigest()
        return f"qq:{digest[:60]}"

    @staticmethod
    def _derive_idempotency_key(
        event: Any, origin: str, capability: str, arguments: dict[str, Any]
    ) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(
            getattr(message_obj, "message_id", "")
            or getattr(message_obj, "id", "")
            or ""
        )
        if not message_id:
            return f"generated:{uuid.uuid4()}"
        digest = hashlib.sha256(
            origin.encode("utf-8")
            + b"\n"
            + message_id.encode("utf-8")
            + b"\n"
            + capability.encode("utf-8")
            + b"\n"
            + canonical_json(arguments)
        ).hexdigest()
        return f"event:{digest}"

    @staticmethod
    def _denied(code: str, message: str) -> str:
        return _json_result(
            {
                "ok": False,
                "error": {"code": code, "message": message, "retryable": False},
            }
        )


def _authorized_identity(
    context: ContextWrapper[AstrAgentContext], authorized_sender_ids: frozenset[str]
) -> tuple[tuple[Any, str, str] | None, str | None]:
    event = DshExecuteTool._event_from_context(context)
    sender_id = DshExecuteTool._event_value(event, "get_sender_id", "sender_id")
    origin = DshExecuteTool._event_value(event, "unified_msg_origin")
    message = str(getattr(event, "message_str", "") or "").strip()
    if not event or not sender_id or not origin or not message or not DshExecuteTool._is_real_user_event(event):
        return None, DshExecuteTool._denied("DIRECT_USER_REQUIRED", "只允许真实用户消息触发 DSH")
    if sender_id not in authorized_sender_ids:
        return None, DshExecuteTool._denied("SENDER_NOT_AUTHORIZED", "当前用户无权调用 DSH")
    return (event, sender_id, origin), None


@dataclass
class FeiyuTaskSubmitTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    service: BridgeService = field(default=None)
    authorized_sender_ids: frozenset[str] = field(default_factory=frozenset)
    default_timeout_seconds: int = 3600
    max_timeout_seconds: int = 86400
    name: str = "feiyu_task_submit"
    description: str = "异步提交受信任的 Windows DSH 任务，立即返回持久化 task_id，不等待执行完成。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1, "description": "交给 DSH Agent 的自然语言任务。"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        prompt: str,
        timeout_seconds: int | None = None,
    ) -> ToolExecResult:
        identity, denied = _authorized_identity(context, self.authorized_sender_ids)
        if denied:
            return denied
        event, sender_id, origin = identity
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            return DshExecuteTool._denied("INVALID_REQUEST", "prompt 必须是非空字符串且不能包含 NUL")
        if timeout_seconds is not None and (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)):
            return DshExecuteTool._denied("INVALID_REQUEST", "timeout_seconds 必须是整数")
        timeout = self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        timeout = min(max(1, int(timeout)), self.max_timeout_seconds)
        capability = "dsh.workflow.run"
        arguments = {
            "prompt": prompt,
            "session_key": DshExecuteTool._derive_session_key(sender_id, origin),
            "timeout_seconds": timeout,
        }
        key = DshExecuteTool._derive_idempotency_key(event, origin, capability, arguments)
        try:
            task = await self.service.create_task(
                capability=capability,
                arguments=arguments,
                origin=origin,
                idempotency_key=key,
                timeout_seconds=timeout,
                trusted_qq_id=sender_id,
                qq_session_id=origin,
                notify_on_terminal=True,
                require_online=False,
            )
            return _json_result({"ok": True, **task.public(include_result=False)})
        except BridgeError as exc:
            return _bridge_error(exc)


@dataclass
class FeiyuTaskStatusTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    service: BridgeService = field(default=None)
    authorized_sender_ids: frozenset[str] = field(default_factory=frozenset)
    name: str = "feiyu_task_status"
    description: str = "查询当前可信操作者已有的异步任务，不会重新提交或执行任务。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"task_id": {"type": "string", "format": "uuid"}},
            "required": ["task_id"],
            "additionalProperties": False,
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], task_id: str) -> ToolExecResult:
        identity, denied = _authorized_identity(context, self.authorized_sender_ids)
        if denied:
            return denied
        _, sender_id, _ = identity
        try:
            return _json_result({"ok": True, **self.service.task_status(task_id, owner=sender_id)})
        except BridgeError as exc:
            return _bridge_error(exc)


@dataclass
class FeiyuTaskListTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    service: BridgeService = field(default=None)
    authorized_sender_ids: frozenset[str] = field(default_factory=frozenset)
    name: str = "feiyu_task_list"
    description: str = "列出当前可信操作者已有任务，可按状态与创建时间筛选。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": [state for state in ("queued", "running", "completed", "failed", "cancel_requested", "cancelled", "timed_out", "unknown")]},
                "created_after": {"type": "integer", "description": "Unix 秒时间戳（含）。"},
                "created_before": {"type": "integer", "description": "Unix 秒时间戳（含）。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        status: str | None = None,
        created_after: int | None = None,
        created_before: int | None = None,
        limit: int = 20,
    ) -> ToolExecResult:
        identity, denied = _authorized_identity(context, self.authorized_sender_ids)
        if denied:
            return denied
        _, sender_id, _ = identity
        if isinstance(limit, bool) or not isinstance(limit, int):
            return DshExecuteTool._denied("INVALID_REQUEST", "limit 必须是整数")
        for label, value in (("created_after", created_after), ("created_before", created_before)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                return DshExecuteTool._denied("INVALID_REQUEST", f"{label} 必须是非负 Unix 秒时间戳")
        if created_after is not None and created_before is not None and created_after > created_before:
            return DshExecuteTool._denied("INVALID_REQUEST", "created_after 不能晚于 created_before")
        try:
            tasks = self.service.list_tasks(
                owner=sender_id,
                status=status,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
            )
            return _json_result({"ok": True, "tasks": tasks, "count": len(tasks)})
        except BridgeError as exc:
            return _bridge_error(exc)


@dataclass
class FeiyuTaskCancelTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    service: BridgeService = field(default=None)
    authorized_sender_ids: frozenset[str] = field(default_factory=frozenset)
    name: str = "feiyu_task_cancel"
    description: str = "请求取消已有任务；运行中任务只有收到 DSH 停止确认后才会显示 cancelled。"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"task_id": {"type": "string", "format": "uuid"}},
            "required": ["task_id"],
            "additionalProperties": False,
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], task_id: str) -> ToolExecResult:
        identity, denied = _authorized_identity(context, self.authorized_sender_ids)
        if denied:
            return denied
        _, sender_id, _ = identity
        try:
            self.service.task_status(task_id, owner=sender_id)
            task = await self.service.cancel_task(task_id)
            return _json_result(
                {
                    "ok": True,
                    "cancel_request_accepted": task.state.value in {"cancel_requested", "cancelled"},
                    "execution_stopped": task.state.value == "cancelled",
                    **task.public(include_result=False),
                }
            )
        except BridgeError as exc:
            return _bridge_error(exc)


def _bridge_error(exc: BridgeError) -> str:
    return _json_result(
        {"ok": False, "error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}}
    )

