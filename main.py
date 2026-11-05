from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .bridge import (
    AutonomousPolicy,
    AutonomousTaskManager,
    BridgeAuthenticator,
    BridgeService,
    TaskNotificationManager,
)
from .bridge.protocol import BRIDGE_CAPABILITIES
from .bridge.tools import (
    DshExecuteTool,
    FeiyuTaskCancelTool,
    FeiyuTaskListTool,
    FeiyuTaskStatusTool,
    FeiyuTaskSubmitTool,
    GetBodyStatusTool,
)
from .bridge.web import BridgeWebApi


class FeiyuBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._terminating = False

        client_id = str(config.get("client_id", "windows-dsh")).strip()
        requested_capabilities = {
            str(item).strip().lower()
            for item in config.get("allowed_capabilities", [])
            if str(item).strip()
        }
        allowed_capabilities = requested_capabilities & BRIDGE_CAPABILITIES
        ignored_capabilities = requested_capabilities - BRIDGE_CAPABILITIES
        self.authenticator = BridgeAuthenticator(
            secret=str(config.get("bridge_secret", "")),
            client_id=client_id,
            clock_skew_seconds=int(config.get("auth_clock_skew_seconds", 60)),
        )
        data_dir = StarTools.get_data_dir("astrbot_plugin_feiyu_bridge")
        self.service = BridgeService(
            client_id=client_id,
            allowed_capabilities=allowed_capabilities,
            heartbeat_timeout_seconds=int(
                config.get("heartbeat_timeout_seconds", 25)
            ),
            max_queue_size=int(config.get("max_queue_size", 8)),
            max_payload_bytes=int(config.get("max_payload_bytes", 262_144)),
            task_retention_seconds=int(config.get("task_retention_seconds", 300)),
            database_path=data_dir / "tasks.sqlite3",
            recovery_grace_seconds=int(config.get("recovery_grace_seconds", 30)),
        )

        self.web_api = BridgeWebApi(self, self.authenticator, self.service)
        self.web_api.register_routes()

        self.autonomous = AutonomousTaskManager(
            service=self.service,
            policy=AutonomousPolicy(
                enabled=bool(config.get("autonomous_enabled", False)),
                provider_id=str(config.get("autonomous_provider_id", "")),
                timezone=str(config.get("autonomous_timezone", "Europe/Berlin")),
                quiet_hours=str(config.get("autonomous_quiet_hours", "2-6")),
                check_interval_minutes=int(
                    config.get("autonomous_check_interval_minutes", 180)
                ),
                min_interval_minutes=int(
                    config.get("autonomous_min_interval_minutes", 360)
                ),
                daily_task_limit=int(config.get("autonomous_daily_task_limit", 2)),
                daily_write_limit=int(config.get("autonomous_daily_write_limit", 1)),
                timeout_seconds=int(config.get("autonomous_timeout_seconds", 45)),
                retry_base_minutes=int(
                    config.get("autonomous_retry_base_minutes", 15)
                ),
                retry_max_minutes=int(
                    config.get("autonomous_retry_max_minutes", 360)
                ),
                read_limit=int(config.get("autonomous_read_limit", 3)),
            ),
            decision_call=self._autonomous_decision,
            data_dir=data_dir,
            audit_call=self._log_autonomous_audit,
        )
        self.autonomous.start()

        authorized_sender_ids = frozenset(
            str(item).strip()
            for item in config.get("authorized_sender_ids", [])
            if str(item).strip()
        )
        self.context.add_llm_tools(
            GetBodyStatusTool(
                service=self.service,
                authorized_sender_ids=authorized_sender_ids,
            ),
            DshExecuteTool(
                service=self.service,
                authorized_sender_ids=authorized_sender_ids,
                default_timeout_seconds=int(
                    config.get("request_timeout_seconds", 30)
                ),
                max_timeout_seconds=int(
                    config.get("max_request_timeout_seconds", 120)
                ),
            ),
            FeiyuTaskSubmitTool(
                service=self.service,
                authorized_sender_ids=authorized_sender_ids,
                default_timeout_seconds=int(config.get("async_task_timeout_seconds", 3600)),
                max_timeout_seconds=int(config.get("async_task_max_timeout_seconds", 86400)),
            ),
            FeiyuTaskStatusTool(service=self.service, authorized_sender_ids=authorized_sender_ids),
            FeiyuTaskListTool(service=self.service, authorized_sender_ids=authorized_sender_ids),
            FeiyuTaskCancelTool(service=self.service, authorized_sender_ids=authorized_sender_ids),
        )

        self.notifications = TaskNotificationManager(
            service=self.service,
            send_call=self._send_task_notification,
            poll_seconds=int(config.get("notification_poll_seconds", 5)),
            max_attempts=int(config.get("notification_max_attempts", 3)),
            retry_delay_seconds=int(config.get("notification_retry_seconds", 30)),
        )
        self.notifications.start()

        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if not self.authenticator.configured:
            logger.warning(
                "Feiyu Bridge HMAC 未配置或少于 32 字符；HTTP Agent 请求将全部拒绝。"
            )
        if not allowed_capabilities:
            logger.warning(
                "Feiyu Bridge 能力白名单为空；dsh_execute 将安全地拒绝所有任务。"
            )
        if ignored_capabilities:
            logger.warning(
                "Feiyu Bridge 忽略了不受支持的能力配置: %s",
                ", ".join(sorted(ignored_capabilities)),
            )
        logger.info("Feiyu Bridge v0.3.0-dev initialized; persistent async scheduling enabled")

    async def _send_task_notification(self, origin: str, text: str) -> bool:
        chain = MessageChain().message(text)
        return bool(await self.context.send_message(origin, chain))

    async def _autonomous_decision(self, prompt: str) -> str:
        provider_id = self.autonomous.policy.provider_id
        provider = await self.context.provider_manager.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError("configured autonomous provider was not found")
        response = await provider.text_chat(prompt=prompt)
        for field_name in ("completion_text", "text", "content"):
            value = getattr(response, field_name, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(response, str):
            return response
        raise RuntimeError("autonomous provider returned no text")

    @staticmethod
    def _log_autonomous_audit(status: str, payload: dict[str, Any]) -> None:
        logger.info(
            "Feiyu autonomous diary status=%s task_id=%s source=%s action=%s",
            status,
            payload.get("task_id"),
            payload.get("trigger_source"),
            payload.get("action"),
        )

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Use proactive-chat's standard hook only as an unprivileged timing hint."""
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "")
        event_module = str(getattr(event.__class__, "__module__", ""))
        if (
            message_id.startswith("proactive:")
            and "astrbot_plugin_proactive_chat" in event_module
            and getattr(message_obj, "raw_message", None) is None
        ):
            self.autonomous.kick("proactive_after_send")

    async def _cleanup_loop(self) -> None:
        try:
            while not self._terminating:
                await asyncio.sleep(15)
                self.service.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Feiyu Bridge cleanup loop failed")

    async def terminate(self):
        self._terminating = True
        removed_routes = self.web_api.unregister_routes()
        await self.notifications.stop()
        await self.autonomous.stop()
        await self.service.shutdown()
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self.service.close()
        logger.info(
            "Feiyu Bridge terminated; pending tasks were cancelled; removed %d routes",
            removed_routes,
        )

