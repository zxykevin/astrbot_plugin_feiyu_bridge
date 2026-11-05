"""Independent mock Windows client for Feiyu Bridge Protocol v1."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable


def canonical_json(value: Any) -> bytes:
    """Client-side serialization; deliberately independent of server modules."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def bridge_hmac_signature(
    secret: str,
    method: str,
    protocol_path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = "\n".join(
        (
            "FEIYU-BRIDGE-V1",
            method.upper(),
            protocol_path,
            timestamp,
            nonce,
            body_hash,
        )
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class BridgeHttpError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


class TaskCancelled(RuntimeError):
    pass


class BridgeHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        openapi_key: str,
        bridge_secret: str,
        client_id: str,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.openapi_key = openapi_key
        self.client_id = client_id
        self.timeout = timeout
        self.bridge_secret = bridge_secret

    def heartbeat(
        self,
        capabilities: list[str],
        *,
        active_task_id: str | None = None,
        agent_version: str = "mock-1.0",
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/heartbeat",
            {
                "agent_version": agent_version,
                "capabilities": capabilities,
                "active_task_id": active_task_id,
            },
        )

    def lease(self, wait_seconds: float = 20) -> dict[str, Any] | None:
        return self.request(
            "POST", "/v1/tasks/lease", {"wait_seconds": wait_seconds}
        )["task"]

    def task_status(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/tasks/{task_id}")

    def submit_result(self, task_id: str, result: Any) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/tasks/{task_id}/result",
            {"status": "succeeded", "result": result},
        )

    def submit_error(
        self,
        task_id: str,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/tasks/{task_id}/result",
            {
                "status": "failed",
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                },
            },
        )

    def acknowledge_cancel(self, task_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/tasks/{task_id}/cancelled", {})

    def request(
        self, method: str, protocol_path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = b"" if payload is None else canonical_json(payload)
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(18)
        headers = {
            "Content-Type": "application/json",
            "X-Feiyu-Client-Id": self.client_id,
            "X-Feiyu-Timestamp": timestamp,
            "X-Feiyu-Nonce": nonce,
            "X-Feiyu-Signature": bridge_hmac_signature(
                self.bridge_secret,
                method,
                protocol_path,
                timestamp,
                nonce,
                body,
            ),
            "X-Request-Id": secrets.token_hex(12),
        }
        if self.openapi_key:
            headers["Authorization"] = f"Bearer {self.openapi_key}"
        request = urllib.request.Request(
            f"{self.base_url}{protocol_path}",
            data=body if method.upper() != "GET" else None,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = {"message": raw.decode("utf-8", errors="replace")}
                status = exc.code
            finally:
                exc.close()
            raise BridgeHttpError(status, decoded) from exc
        if not decoded.get("ok"):
            raise BridgeHttpError(response.status, decoded)
        return decoded["data"]


Handler = Callable[[dict[str, Any], asyncio.Event], Awaitable[Any]]


class MockWindowsAgent:
    def __init__(
        self,
        client: BridgeHttpClient,
        capabilities: list[str],
        *,
        heartbeat_interval: float = 5,
    ) -> None:
        self.client = client
        self.capabilities = capabilities
        self.heartbeat_interval = heartbeat_interval
        self.active_task_id: str | None = None
        self.cancel_event = asyncio.Event()
        self.handlers: dict[str, Handler] = {
            "dsh.workflow.run": self._mock_workflow,
            "dsh.status.read": self._mock_status,
        }

    async def run(self, *, once: bool = False) -> None:
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            while True:
                task = await asyncio.to_thread(self.client.lease, 20)
                if task is None:
                    if once:
                        return
                    continue
                await self._execute(task)
                if once:
                    return
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self) -> None:
        while True:
            response = await asyncio.to_thread(
                self.client.heartbeat,
                self.capabilities,
                active_task_id=self.active_task_id,
            )
            if response.get("cancel_requested"):
                self.cancel_event.set()
            await asyncio.sleep(self.heartbeat_interval)

    async def _execute(self, task: dict[str, Any]) -> None:
        task_id = str(task["task_id"])
        capability = str(task["capability"])
        handler = self.handlers.get(capability)
        self.active_task_id = task_id
        self.cancel_event.clear()
        try:
            if handler is None:
                await asyncio.to_thread(
                    self.client.submit_error,
                    task_id,
                    code="MOCK_CAPABILITY_UNIMPLEMENTED",
                    message="mock client has no exact handler",
                )
                return
            result = await handler(task.get("arguments", {}), self.cancel_event)
            if self.cancel_event.is_set():
                raise TaskCancelled()
            await asyncio.to_thread(self.client.submit_result, task_id, result)
        except TaskCancelled:
            await asyncio.to_thread(self.client.acknowledge_cancel, task_id)
        except Exception as exc:
            await asyncio.to_thread(
                self.client.submit_error,
                task_id,
                code="MOCK_HANDLER_FAILED",
                message=str(exc)[:256],
            )
        finally:
            self.active_task_id = None
            self.cancel_event.clear()

    @staticmethod
    async def _mock_status(
        arguments: dict[str, Any], cancel_event: asyncio.Event
    ) -> dict[str, Any]:
        del arguments, cancel_event
        return {"mock": True, "online": True, "platform": "windows"}

    @staticmethod
    async def _mock_workflow(
        arguments: dict[str, Any], cancel_event: asyncio.Event
    ) -> dict[str, Any]:
        delay = min(max(float(arguments.get("delay_seconds", 0)), 0), 30)
        deadline = asyncio.get_running_loop().time() + delay
        while asyncio.get_running_loop().time() < deadline:
            if cancel_event.is_set():
                raise TaskCancelled()
            await asyncio.sleep(min(0.2, delay or 0.01))
        return {
            "mock": True,
            "value": arguments.get("prompt"),
            "session_key": arguments.get("session_key"),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_feiyu_bridge",
    )
    parser.add_argument("--client-id", default="windows-dsh")
    parser.add_argument(
        "--capability", action="append", dest="capabilities", default=[]
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    api_key = os.environ.get("FEIYU_ASTRBOT_API_KEY", "")
    secret = os.environ.get("FEIYU_BRIDGE_SECRET", "")
    if not api_key or len(secret) < 32:
        raise SystemExit(
            "FEIYU_ASTRBOT_API_KEY and a 32+ character FEIYU_BRIDGE_SECRET are required"
        )
    capabilities = args.capabilities or ["dsh.workflow.run", "dsh.status.read"]
    client = BridgeHttpClient(
        base_url=args.base_url,
        openapi_key=api_key,
        bridge_secret=secret,
        client_id=args.client_id,
    )
    await MockWindowsAgent(client, capabilities).run(once=args.once)


if __name__ == "__main__":
    asyncio.run(main())

