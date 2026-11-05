from __future__ import annotations

import asyncio
import json
import re
import logging
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# The black-box harness exercises the real Bridge Web API class without requiring
# AstrBot to be installed in the local unit-test interpreter.
if "astrbot.api" not in sys.modules:
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    web_api_module = types.ModuleType("astrbot.api.web")
    api_module.logger = logging.getLogger("feiyu-http-harness")
    web_api_module.request = None
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.web"] = web_api_module

import bridge.web as web_module
from bridge.auth import BridgeAuthenticator
from bridge.service import BridgeService
from bridge.web import BridgeWebApi

EXTERNAL_PREFIX = (
    "/api/v1/plugins/extensions/astrbot_plugin_feiyu_bridge"
)


class CaseInsensitiveHeaders(dict):
    def __init__(self, source):
        super().__init__((str(key).lower(), str(value)) for key, value in source.items())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class FakeRequest:
    def __init__(self, headers, body: bytes):
        self.headers = CaseInsensitiveHeaders(headers)
        self._body = body

    async def body(self) -> bytes:
        return self._body


class FakeContext:
    def __init__(self):
        self.registered_web_apis: list[tuple] = []

    def register_web_api(self, route, handler, methods, desc):
        for index, api in enumerate(self.registered_web_apis):
            if api[0] == route and api[2] == methods:
                self.registered_web_apis[index] = (route, handler, methods, desc)
                return
        self.registered_web_apis.append((route, handler, methods, desc))


class FakePlugin:
    def __init__(self):
        self.context = FakeContext()
        self._terminating = False


def _route_pattern(route: str) -> re.Pattern[str]:
    chunks: list[str] = []
    position = 0
    for match in re.finditer(r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>", route):
        chunks.append(re.escape(route[position : match.start()]))
        chunks.append(
            f"(?P<{match.group(2)}>.*)"
            if match.group(1)
            else f"(?P<{match.group(2)}>[^/]+)"
        )
        position = match.end()
    chunks.append(re.escape(route[position:]))
    return re.compile("".join(chunks))


class _Handler(BaseHTTPRequestHandler):
    server_version = "FeiyuAstrBotHarness/1"

    def do_GET(self):
        self.server.harness.handle(self)  # type: ignore[attr-defined]

    def do_POST(self):
        self.server.harness.handle(self)  # type: ignore[attr-defined]

    def log_message(self, _format, *_args):
        return


class AstrBotRouteHarness:
    """Real HTTP harness for Bridge routes with AstrBot-like scope enforcement."""

    def __init__(self):
        self.plugin = FakePlugin()
        self.secret = "harness-bridge-secret-32-characters!!"
        self.client_id = "windows-dsh-test"
        self.authenticator = BridgeAuthenticator(self.secret, self.client_id)
        self.service = BridgeService(
            client_id=self.client_id,
            allowed_capabilities={"dsh.workflow.run", "dsh.status.read"},
            heartbeat_timeout_seconds=25,
        )
        self.web = BridgeWebApi(
            self.plugin, self.authenticator, self.service
        )
        self.web.register_routes()
        self.keys = {
            "plugin-test-key": {"plugin"},
            "chat-test-key": {"chat"},
        }
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.harness = self  # type: ignore[attr-defined]
        self.http_thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.http_thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{EXTERNAL_PREFIX}"

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        token = self._api_key(handler)
        if not token or token not in self.keys:
            self._respond(handler, 401, {"detail": "Invalid or missing API key"})
            return
        if "plugin" not in self.keys[token]:
            self._respond(handler, 403, {"detail": "Insufficient API key scope"})
            return
        if not handler.path.startswith(EXTERNAL_PREFIX):
            self._respond(handler, 404, {"detail": "Not found"})
            return

        plugin_path = "/astrbot_plugin_feiyu_bridge" + handler.path[len(EXTERNAL_PREFIX) :]
        matched = None
        for route, view_handler, methods, _desc in self.plugin.context.registered_web_apis:
            if handler.command not in methods:
                continue
            match = _route_pattern(route).fullmatch(plugin_path)
            if match:
                matched = (view_handler, match.groupdict())
                break
        if not matched:
            self._respond(handler, 404, {"detail": "Plugin route not found"})
            return

        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length) if length else b""
        view_handler, path_values = matched

        async def invoke():
            web_module.request = FakeRequest(handler.headers, body)
            return await view_handler(**path_values)

        try:
            response = self.submit(invoke()).result(timeout=35)
            if isinstance(response, tuple):
                payload, status = response[0], int(response[1])
            else:
                payload, status = response, 200
            self._respond(handler, status, payload)
        except Exception as exc:
            self._respond(handler, 500, {"detail": type(exc).__name__})

    @staticmethod
    def _api_key(handler: BaseHTTPRequestHandler) -> str:
        auth = str(handler.headers.get("Authorization", ""))
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return str(handler.headers.get("X-API-Key", "")).strip()

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.http_thread.join(timeout=2)
        self.submit(self.service.shutdown()).result(timeout=2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

