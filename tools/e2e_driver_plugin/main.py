"""Isolation-only control plugin for real Windows DSH integration tests."""

from __future__ import annotations

import hmac
import json
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star
from astrbot.api.web import request


PLUGIN_NAME = "astrbot_plugin_feiyu_bridge_e2e_driver"
ROUTE_PREFIX = f"/{PLUGIN_NAME}/v1"


class FeiyuBridgeE2EDriver(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.control_secret = str(config.get("control_secret", ""))
        self._routes = []
        self._register("/enqueue", self.enqueue, ["POST"])
        self._register("/tasks/<task_id>", self.task, ["GET"])
        self._register("/tasks/<task_id>/cancel", self.cancel, ["POST"])
        logger.warning("Feiyu Bridge E2E Driver loaded; isolation use only")

    def _register(self, suffix, handler, methods):
        route = f"{ROUTE_PREFIX}{suffix}"
        self.context.register_web_api(route, handler, methods, "isolation-only E2E")
        self._routes.append((route, handler, methods))

    def _service(self):
        metadata = self.context.get_registered_star("astrbot_plugin_feiyu_bridge")
        plugin = getattr(metadata, "star_cls", None) if metadata else None
        service = getattr(plugin, "service", None)
        if service is None:
            raise RuntimeError("Bridge plugin service is unavailable")
        return service

    def _authorized(self) -> bool:
        supplied = str(request.headers.get("X-E2E-Control", ""))
        return len(self.control_secret) >= 32 and hmac.compare_digest(
            supplied, self.control_secret
        )

    async def _body(self) -> dict[str, Any]:
        raw = await request.body()
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise ValueError("body must be an object")
        return value

    async def enqueue(self):
        if not self._authorized():
            return {"ok": False, "error": "unauthorized"}, 401
        try:
            body = await self._body()
            timeout = int(body.get("timeout_seconds", 30))
            task = await self._service().create_task(
                capability="dsh.workflow.run",
                arguments={
                    "prompt": body["prompt"],
                    "session_key": body["session_key"],
                    "timeout_seconds": timeout,
                },
                origin="isolated-e2e-driver",
                idempotency_key=body["idempotency_key"],
                timeout_seconds=timeout,
            )
            return {"ok": True, "task": task.public(include_arguments=True)}, 200
        except Exception as exc:
            code = str(getattr(exc, "code", "E2E_DRIVER_ERROR"))
            return {"ok": False, "error": code}, 409

    async def task(self, task_id: str):
        if not self._authorized():
            return {"ok": False, "error": "unauthorized"}, 401
        service = self._service()
        service.task_status(task_id)
        internal = service._tasks[task_id]  # isolation-only test introspection
        return {
            "ok": True,
            "task": {
                **internal.public(),
                "result": internal.result,
                "error": internal.error,
            },
        }, 200

    async def cancel(self, task_id: str):
        if not self._authorized():
            return {"ok": False, "error": "unauthorized"}, 401
        task = await self._service().cancel_task(task_id)
        return {"ok": True, "task": task.public()}, 200

    async def terminate(self):
        registry = getattr(self.context, "registered_web_apis", None)
        if not isinstance(registry, list):
            return
        owned = {
            (route, tuple(methods), id(handler))
            for route, handler, methods in self._routes
        }
        registry[:] = [
            item
            for item in registry
            if not (
                len(item) >= 3
                and (item[0], tuple(item[2]), id(item[1])) in owned
            )
        ]
