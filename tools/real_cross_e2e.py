"""Run credential-redacted E2E checks against the isolated real DSH/AstrBot pair."""

from __future__ import annotations

import hashlib
import hmac
import argparse
import json
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


BASE = os.environ.get("FEIYU_E2E_BASE_URL", "http://127.0.0.1:16187").rstrip("/")
BRIDGE_PREFIX = "/api/v1/plugins/extensions/astrbot_plugin_feiyu_bridge"
DRIVER_PREFIX = (
    "/api/v1/plugins/extensions/astrbot_plugin_feiyu_bridge_e2e_driver/v1"
)
CLIENT_ID = "windows-dsh-e2e"
API_KEY = os.environ["FEIYU_E2E_API_KEY"]
BRIDGE_SECRET = os.environ["FEIYU_E2E_BRIDGE_SECRET"]
CONTROL_SECRET = os.environ["FEIYU_E2E_CONTROL_SECRET"]


class HttpFailure(RuntimeError):
    def __init__(self, status: int, payload: Any):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.payload = payload


def _request(path: str, *, method="GET", body=None, headers=None):
    raw = None if body is None else json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=raw,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            raise HttpFailure(exc.code, payload) from None
        finally:
            exc.close()


def bridge_request(method: str, protocol_path: str, body=None):
    raw = b"" if body is None else json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    body_hash = hashlib.sha256(raw).hexdigest()
    canonical = "\n".join(
        (
            "FEIYU-BRIDGE-V1",
            method.upper(),
            protocol_path,
            timestamp,
            nonce,
            body_hash,
        )
    ).encode("utf-8")
    signature = hmac.new(
        BRIDGE_SECRET.encode("utf-8"), canonical, hashlib.sha256
    ).hexdigest()
    status, envelope = _request(
        BRIDGE_PREFIX + protocol_path,
        method=method,
        body=body,
        headers={
            "X-API-Key": API_KEY,
            "X-Feiyu-Client-Id": CLIENT_ID,
            "X-Feiyu-Timestamp": timestamp,
            "X-Feiyu-Nonce": nonce,
            "X-Feiyu-Signature": signature,
        },
    )
    if envelope.get("ok") is not True:
        raise HttpFailure(status, envelope)
    return envelope["data"]


def driver_request(method: str, path: str, body=None, *, control=None):
    status, payload = _request(
        DRIVER_PREFIX + path,
        method=method,
        body=body,
        headers={
            "X-API-Key": API_KEY,
            "X-E2E-Control": CONTROL_SECRET if control is None else control,
        },
    )
    if payload.get("ok") is not True:
        raise HttpFailure(status, payload)
    return payload


def enqueue(prompt: str, session_key: str, timeout: int):
    response = driver_request(
        "POST",
        "/enqueue",
        {
            "prompt": prompt,
            "session_key": session_key,
            "timeout_seconds": timeout,
            "idempotency_key": f"e2e:{uuid.uuid4()}",
        },
    )
    return response["task"]["task_id"]


def task(task_id: str):
    return driver_request("GET", f"/tasks/{task_id}")["task"]


def wait_state(task_id: str, states: set[str], timeout: float):
    deadline = time.monotonic() + timeout
    current = None
    while time.monotonic() < deadline:
        current = task(task_id)
        if current["state"] in states:
            return current
        time.sleep(0.25)
    raise TimeoutError(f"task did not reach {sorted(states)}; last={current}")


def safe_value(record: dict[str, Any]) -> str:
    result = record.get("result") or {}
    return str(result.get("value", ""))[:500]


def cancel_only() -> None:
    status = bridge_request("GET", "/v1/status")
    if not status["online"]:
        raise RuntimeError("Windows client is offline")
    task_id = enqueue(
        "逐行输出从 1 到 10000 的每一个整数，每行只能有一个整数，"
        "不得省略、概括、使用代码或提前结束。",
        "qq:e2e-cancel-long",
        90,
    )
    leased = wait_state(task_id, {"leased", "succeeded", "failed"}, 20)
    if leased["state"] == "leased":
        driver_request("POST", f"/tasks/{task_id}/cancel", {})
    terminal = wait_state(
        task_id, {"cancelled", "succeeded", "failed", "timed_out"}, 30
    )
    print(
        json.dumps(
            {
                "cancel_task": {
                    "id": task_id,
                    "state_before_cancel": leased["state"],
                    "terminal_state": terminal["state"],
                }
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def status_only(wait_online: int = 0) -> None:
    deadline = time.monotonic() + wait_online
    while True:
        status = bridge_request("GET", "/v1/status")
        if status["online"] or time.monotonic() >= deadline:
            print(
                json.dumps(
                    {
                        "online": status["online"],
                        "heartbeat_age_seconds": status[
                            "last_heartbeat_age_seconds"
                        ],
                        "capabilities": status["capabilities"],
                    },
                    ensure_ascii=False,
                )
            )
            return
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cancel-only", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--wait-online", type=int, default=0)
    args = parser.parse_args()
    if args.cancel_only:
        cancel_only()
        return
    if args.status_only:
        status_only(args.wait_online)
        return

    report: dict[str, Any] = {"protocol": "feiyu-bridge/1"}

    try:
        _request(BRIDGE_PREFIX + "/v1/status")
    except HttpFailure as exc:
        report["missing_api_key_http"] = exc.status
    try:
        driver_request("GET", "/tasks/00000000-0000-0000-0000-000000000000", control="wrong")
    except HttpFailure as exc:
        report["driver_control_rejected_http"] = exc.status

    status = bridge_request("GET", "/v1/status")
    report["online"] = status["online"]
    report["capabilities"] = status["capabilities"]
    report["agent_version"] = status["agent_version"]

    session_a = "qq:e2e-session-a"
    first_id = enqueue(
        "这是跨系统测试第一轮。请只回复标记 BRIDGE_E2E_TURN1。",
        session_a,
        90,
    )
    first = wait_state(first_id, {"succeeded", "failed", "timed_out"}, 120)
    report["first_task"] = {
        "id": first_id,
        "state": first["state"],
        "reply": safe_value(first),
        "turn": (first.get("result") or {}).get("turn"),
    }

    second_id = enqueue(
        "请复述上一轮要求你回复的标记，只输出那个标记。",
        session_a,
        90,
    )
    second = wait_state(second_id, {"succeeded", "failed", "timed_out"}, 120)
    report["same_session_task"] = {
        "id": second_id,
        "state": second["state"],
        "reply": safe_value(second),
        "turn": (second.get("result") or {}).get("turn"),
    }

    isolated_id = enqueue(
        "如果本会话知道 BRIDGE_E2E_TURN1 就回复 LEAK；否则只回复 ISOLATED。",
        "qq:e2e-session-b",
        90,
    )
    isolated = wait_state(
        isolated_id, {"succeeded", "failed", "timed_out"}, 120
    )
    report["isolated_session_task"] = {
        "id": isolated_id,
        "state": isolated["state"],
        "reply": safe_value(isolated),
        "turn": (isolated.get("result") or {}).get("turn"),
    }

    if first["state"] == "succeeded":
        result = first["result"]
        retried = bridge_request(
            "POST",
            f"/v1/tasks/{first_id}/result",
            {"status": "succeeded", "result": result},
        )
        report["identical_result_retry"] = retried["state"]
        try:
            bridge_request(
                "POST",
                f"/v1/tasks/{first_id}/result",
                {"status": "succeeded", "result": {"value": "conflict"}},
            )
        except HttpFailure as exc:
            report["conflicting_result_error"] = (
                exc.payload.get("error", {}).get("code")
            )

    cancel_id = enqueue(
        "请生成一篇至少八千字的长文，在完成前不要给出简短答案。",
        "qq:e2e-cancel",
        90,
    )
    wait_state(cancel_id, {"leased", "succeeded", "failed"}, 20)
    driver_request("POST", f"/tasks/{cancel_id}/cancel", {})
    cancelled = wait_state(cancel_id, {"cancelled", "succeeded", "failed"}, 30)
    report["cancel_task"] = {"id": cancel_id, "state": cancelled["state"]}

    timeout_id = enqueue(
        "请生成一篇至少八千字的长文，在完成前不要给出简短答案。",
        "qq:e2e-timeout",
        2,
    )
    timed_out = wait_state(
        timeout_id, {"timed_out", "succeeded", "failed"}, 30
    )
    report["timeout_task"] = {"id": timeout_id, "state": timed_out["state"]}

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
