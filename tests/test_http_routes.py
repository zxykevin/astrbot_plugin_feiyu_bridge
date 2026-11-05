import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from http_harness import AstrBotRouteHarness
from mock_windows_client import (
    BridgeHttpClient,
    BridgeHttpError,
    bridge_hmac_signature,
)


class BridgeHttpRouteTests(unittest.TestCase):
    def setUp(self):
        self.harness = AstrBotRouteHarness()
        self.client = self.make_client("plugin-test-key")

    def tearDown(self):
        self.harness.close()

    def make_client(self, key, secret=None):
        return BridgeHttpClient(
            base_url=self.harness.base_url,
            openapi_key=key,
            bridge_secret=secret or self.harness.secret,
            client_id=self.harness.client_id,
            timeout=3,
        )

    def heartbeat(self, client=None, active_task_id=None):
        return (client or self.client).heartbeat(
            ["dsh.workflow.run", "dsh.status.read"],
            active_task_id=active_task_id,
        )

    def raw_heartbeat(self, body, *, signed_body=None, timestamp=None, nonce=None):
        timestamp = str(timestamp if timestamp is not None else int(time.time()))
        nonce = nonce or f"httpnegative{time.time_ns()}"
        signed_body = body if signed_body is None else signed_body
        headers = {
            "Authorization": "Bearer plugin-test-key",
            "Content-Type": "application/json",
            "X-Feiyu-Client-Id": self.harness.client_id,
            "X-Feiyu-Timestamp": timestamp,
            "X-Feiyu-Nonce": nonce,
            "X-Feiyu-Signature": bridge_hmac_signature(
                self.harness.secret,
                "POST",
                "/v1/heartbeat",
                timestamp,
                nonce,
                signed_body,
            ),
        }
        request = urllib.request.Request(
            f"{self.harness.base_url}/v1/heartbeat",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_openapi_key_is_required_before_bridge_hmac(self):
        with self.assertRaises(BridgeHttpError) as raised:
            self.heartbeat(self.make_client(""))
        self.assertEqual(raised.exception.status, 401)
        self.assertNotIn("protocol", raised.exception.payload)

    def test_openapi_plugin_scope_is_required(self):
        with self.assertRaises(BridgeHttpError) as raised:
            self.heartbeat(self.make_client("chat-test-key"))
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("scope", raised.exception.payload["detail"].lower())

    def test_bridge_hmac_is_required_after_openapi_auth(self):
        with self.assertRaises(BridgeHttpError) as raised:
            self.heartbeat(self.make_client("plugin-test-key", "x" * 32))
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(
            raised.exception.payload["error"]["code"], "AUTH_INVALID_SIGNATURE"
        )

    def test_valid_double_auth_heartbeat(self):
        status = self.heartbeat()
        self.assertTrue(status["online"])
        self.assertEqual(
            status["capabilities"], ["dsh.status.read", "dsh.workflow.run"]
        )

    def test_http_body_tampering_is_rejected(self):
        original = b'{"agent_version":"test","capabilities":[]}'
        tampered = b'{"agent_version":"test","capabilities":["dsh.status.read"]}'
        status, payload = self.raw_heartbeat(tampered, signed_body=original)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "AUTH_INVALID_SIGNATURE")

    def test_http_expired_timestamp_is_rejected(self):
        body = b'{"agent_version":"test","capabilities":[]}'
        status, payload = self.raw_heartbeat(
            body, timestamp=int(time.time()) - 120
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "AUTH_TIMESTAMP_EXPIRED")

    def test_http_repeated_nonce_is_rejected(self):
        body = b'{"agent_version":"test","capabilities":[]}'
        timestamp = int(time.time())
        nonce = f"httpreplay{time.time_ns()}"
        first_status, _ = self.raw_heartbeat(
            body, timestamp=timestamp, nonce=nonce
        )
        second_status, payload = self.raw_heartbeat(
            body, timestamp=timestamp, nonce=nonce
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 401)
        self.assertEqual(payload["error"]["code"], "AUTH_REPLAY")

    def test_real_http_task_round_trip(self):
        self.heartbeat()
        task = self.harness.submit(
            self.harness.service.create_task(
                capability="dsh.workflow.run",
                arguments={
                    "prompt": "integration",
                    "session_key": "qq:http-roundtrip",
                    "timeout_seconds": 10,
                },
                origin="test:FriendMessage:1",
                idempotency_key="http:roundtrip01",
                timeout_seconds=10,
            )
        ).result(timeout=2)
        leased = self.client.lease(0)
        self.assertEqual(leased["task_id"], task.task_id)
        result_body = {"mock": True, "value": 7}
        first_terminal = self.client.submit_result(task.task_id, result_body)
        retried_terminal = self.client.submit_result(task.task_id, result_body)
        self.assertEqual(first_terminal["state"], "succeeded")
        self.assertEqual(retried_terminal["state"], "succeeded")
        with self.assertRaises(BridgeHttpError) as raised:
            self.client.submit_result(task.task_id, {"mock": True, "value": 8})
        self.assertEqual(
            raised.exception.payload["error"]["code"], "TASK_ALREADY_TERMINAL"
        )
        result = self.harness.submit(
            self.harness.service.wait_for_result(task)
        ).result(timeout=2)
        self.assertEqual(result, result_body)

    def test_http_cancel_confirmation_releases_lease(self):
        self.heartbeat()
        task = self.harness.submit(
            self.harness.service.create_task(
                capability="dsh.workflow.run",
                arguments={
                    "prompt": "cancel me",
                    "session_key": "qq:http-cancel",
                    "timeout_seconds": 10,
                },
                origin="test:FriendMessage:1",
                idempotency_key="http:cancel001",
                timeout_seconds=10,
            )
        ).result(timeout=2)
        self.client.lease(0)
        self.harness.submit(
            self.harness.service.cancel_task(task.task_id)
        ).result(timeout=2)
        status = self.heartbeat(active_task_id=task.task_id)
        self.assertTrue(status["cancel_requested"])
        acknowledged = self.client.acknowledge_cancel(task.task_id)
        self.assertEqual(acknowledged["state"], "cancelled")
        retried = self.client.acknowledge_cancel(task.task_id)
        self.assertEqual(retried["state"], "cancelled")

    def test_unregister_removes_only_owned_routes(self):
        self.harness.plugin.context.register_web_api(
            "/other_plugin/status", lambda: None, ["GET"], "other"
        )
        before = len(self.harness.plugin.context.registered_web_apis)
        removed = self.harness.web.unregister_routes()
        self.assertEqual(removed, 6)
        self.assertEqual(
            len(self.harness.plugin.context.registered_web_apis), before - 6
        )
        self.assertEqual(
            self.harness.plugin.context.registered_web_apis[0][0],
            "/other_plugin/status",
        )
        with self.assertRaises(BridgeHttpError) as raised:
            self.heartbeat()
        self.assertEqual(raised.exception.status, 404)


if __name__ == "__main__":
    unittest.main()

