from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.web import request

from .auth import AuthenticationError, BridgeAuthenticator
from .protocol import PROTOCOL_ID, error, ok
from .service import BridgeError, BridgeService

PLUGIN_NAME = "astrbot_plugin_feiyu_bridge"
ROUTE_PREFIX = f"/{PLUGIN_NAME}/v1"


class BridgeWebApi:
    def __init__(
        self,
        plugin: Any,
        authenticator: BridgeAuthenticator,
        service: BridgeService,
    ) -> None:
        self.plugin = plugin
        self.authenticator = authenticator
        self.service = service
        self.max_payload_bytes = service.max_payload_bytes
        self._registered_routes: list[
            tuple[str, Callable[..., Awaitable[Any]], list[str]]
        ] = []

    def register_routes(self) -> None:
        routes = (
            ("/status", self.status, ["GET"], "Feiyu Bridge status"),
            ("/heartbeat", self.heartbeat, ["POST"], "Feiyu Bridge heartbeat"),
            ("/tasks/lease", self.lease_task, ["POST"], "Lease next Bridge task"),
            (
                "/tasks/<task_id>",
                self.task_status,
                ["GET"],
                "Get Bridge task status",
            ),
            (
                "/tasks/<task_id>/result",
                self.task_result,
                ["POST"],
                "Submit Bridge task result",
            ),
            (
                "/tasks/<task_id>/cancelled",
                self.task_cancelled,
                ["POST"],
                "Acknowledge Bridge task cancellation",
            ),
        )
        for suffix, handler, methods, description in routes:
            route = f"{ROUTE_PREFIX}{suffix}"
            self.plugin.context.register_web_api(
                route, handler, methods, description
            )
            self._registered_routes.append((route, handler, methods))

    def unregister_routes(self) -> int:
        """Best-effort cleanup for AstrBot 4.29.4, which has no public unregister API.

        Handler identity is checked so an old plugin instance cannot remove a route
        that a newer instance has already replaced during hot reload.
        """
        registry = getattr(self.plugin.context, "registered_web_apis", None)
        if not isinstance(registry, list):
            return 0
        owned = {
            (route, tuple(methods), id(handler))
            for route, handler, methods in self._registered_routes
        }
        retained = [
            api
            for api in registry
            if not (
                len(api) >= 3
                and (api[0], tuple(api[2]), id(api[1])) in owned
            )
        ]
        removed = len(registry) - len(retained)
        registry[:] = retained
        self._registered_routes.clear()
        return removed

    async def status(self):
        return await self._handle("GET", "/v1/status", self._status)

    async def heartbeat(self):
        return await self._handle("POST", "/v1/heartbeat", self._heartbeat)

    async def lease_task(self):
        return await self._handle("POST", "/v1/tasks/lease", self._lease_task)

    async def task_status(self, task_id: str):
        return await self._handle(
            "GET",
            f"/v1/tasks/{task_id}",
            lambda _body, _client: self._task_status(task_id),
        )

    async def task_result(self, task_id: str):
        return await self._handle(
            "POST",
            f"/v1/tasks/{task_id}/result",
            lambda body, client: self._task_result(task_id, body, client),
        )

    async def task_cancelled(self, task_id: str):
        return await self._handle(
            "POST",
            f"/v1/tasks/{task_id}/cancelled",
            lambda body, client: self._task_cancelled(task_id, body, client),
        )

    async def _handle(
        self,
        method: str,
        protocol_path: str,
        callback: Callable[[dict[str, Any], str], Awaitable[Any]],
    ):
        request_id = str(request.headers.get("X-Request-Id", ""))[:128] or None
        if getattr(self.plugin, "_terminating", False):
            return (
                error(
                    "BRIDGE_SHUTTING_DOWN",
                    "bridge is shutting down",
                    retryable=True,
                    request_id=request_id,
                ),
                503,
            )
        try:
            body_raw = await request.body()
            if len(body_raw) > self.max_payload_bytes:
                raise BridgeError("PAYLOAD_TOO_LARGE", "request exceeds size limit")
            client_id = self.authenticator.verify(
                method, protocol_path, request.headers, body_raw
            )
            body = self._decode_json(body_raw)
            data = await callback(body, client_id)
            return ok(data, request_id=request_id), 200
        except AuthenticationError as exc:
            return error(exc.code, str(exc), request_id=request_id), 401
        except BridgeError as exc:
            status = {
                "TASK_NOT_FOUND": 404,
                "QUEUE_FULL": 429,
                "PAYLOAD_TOO_LARGE": 413,
            }.get(exc.code, 409)
            return (
                error(
                    exc.code,
                    str(exc),
                    retryable=exc.retryable,
                    request_id=request_id,
                ),
                status,
            )
        except ValueError as exc:
            return error("INVALID_REQUEST", str(exc), request_id=request_id), 400
        except Exception:
            logger.exception("Feiyu Bridge request failed: %s", protocol_path)
            return error("INTERNAL_ERROR", "internal bridge error", request_id=request_id), 500

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    async def _status(self, _body: dict[str, Any], _client_id: str):
        return {"protocol": PROTOCOL_ID, **self.service.body_status()}

    async def _heartbeat(self, body: dict[str, Any], client_id: str):
        capabilities = body.get("capabilities", [])
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise ValueError("capabilities must be a string array")
        pending_task_ids = body.get("pending_task_ids", [])
        if (
            not isinstance(pending_task_ids, list)
            or len(pending_task_ids) > 50
            or not all(isinstance(item, str) for item in pending_task_ids)
        ):
            raise ValueError("pending_task_ids must be a string array of at most 50 items")
        return await self.service.heartbeat(
            client_id=client_id,
            capabilities=capabilities,
            agent_version=str(body.get("agent_version", "")),
            active_task_id=(
                str(body["active_task_id"]) if body.get("active_task_id") else None
            ),
            pending_task_ids=pending_task_ids,
        )

    async def _lease_task(self, body: dict[str, Any], client_id: str):
        leased = await self.service.lease_task(
            client_id=client_id,
            wait_seconds=float(body.get("wait_seconds", 20)),
        )
        return {"task": leased, "server_time": int(__import__("time").time())}

    async def _task_status(self, task_id: str):
        return self.service.task_status(task_id, wire=True)

    async def _task_result(
        self, task_id: str, body: dict[str, Any], client_id: str
    ):
        task = await self.service.submit_result(
            client_id=client_id,
            task_id=task_id,
            status=str(body.get("status", "")),
            result=body.get("result"),
            error=body.get("error") if isinstance(body.get("error"), dict) else None,
        )
        return task.public(wire=True)

    async def _task_cancelled(
        self, task_id: str, _body: dict[str, Any], client_id: str
    ):
        task = await self.service.acknowledge_cancellation(
            client_id=client_id, task_id=task_id
        )
        return task.public(wire=True)

