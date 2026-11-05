"""Black-box checks against an isolated real AstrBot 4.29.4 instance."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mock_windows_client import BridgeHttpClient, BridgeHttpError


def json_request(opener, url, payload=None, *, method="POST"):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with opener.open(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def create_key(opener, base_url: str, name: str, scopes: list[str]) -> str:
    response = json_request(
        opener,
        f"{base_url}/api/apikey/create",
        {"name": name, "scopes": scopes, "expires_in_days": 1},
    )
    if response.get("status") != "ok":
        raise RuntimeError(f"API key creation failed: {response}")
    return str(response["data"]["api_key"])


def expect_http_error(call, status: int, bridge_code: str | None = None):
    try:
        call()
    except BridgeHttpError as exc:
        if exc.status != status:
            raise AssertionError(f"expected HTTP {status}, got {exc.status}") from exc
        if bridge_code:
            actual = exc.payload.get("error", {}).get("code")
            if actual != bridge_code:
                raise AssertionError(
                    f"expected Bridge code {bridge_code}, got {actual}"
                ) from exc
        return
    raise AssertionError(f"expected HTTP {status}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:16186")
    parser.add_argument("--username", default="astrbot")
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = json_request(
        opener,
        f"{args.base_url}/api/auth/login",
        {"username": args.username, "password": args.password},
    )
    if login.get("status") != "ok":
        raise RuntimeError("isolated AstrBot login failed")
    dashboard_token = str(login.get("data", {}).get("token", ""))
    if not dashboard_token:
        raise RuntimeError("isolated AstrBot login returned no dashboard token")
    opener.addheaders = [("Authorization", f"Bearer {dashboard_token}")]

    plugin_key = create_key(opener, args.base_url, "feiyu-e2e-plugin", ["plugin"])
    chat_key = create_key(opener, args.base_url, "feiyu-e2e-chat", ["chat"])
    bridge_base = (
        f"{args.base_url}/api/v1/plugins/extensions/"
        "astrbot_plugin_feiyu_bridge"
    )
    common = {
        "base_url": bridge_base,
        "bridge_secret": "isolated-test-bridge-secret-32-characters!!",
        "client_id": "windows-dsh-isolated",
        "timeout": 10,
    }
    missing_key = BridgeHttpClient(openapi_key="", **common)
    wrong_scope = BridgeHttpClient(openapi_key=chat_key, **common)
    wrong_hmac = BridgeHttpClient(
        openapi_key=plugin_key,
        bridge_secret="wrong-isolated-secret-32-characters!",
        client_id=common["client_id"],
        base_url=common["base_url"],
        timeout=10,
    )
    valid = BridgeHttpClient(openapi_key=plugin_key, **common)
    capabilities = ["dsh.workflow.run", "dsh.status.read"]

    expect_http_error(lambda: missing_key.heartbeat(capabilities), 401)
    expect_http_error(lambda: wrong_scope.heartbeat(capabilities), 403)
    expect_http_error(
        lambda: wrong_hmac.heartbeat(capabilities),
        401,
        "AUTH_INVALID_SIGNATURE",
    )
    heartbeat = valid.heartbeat(capabilities, agent_version="isolated-e2e")
    if not heartbeat.get("online"):
        raise AssertionError("valid heartbeat did not mark Agent online")
    if valid.lease(0) is not None:
        raise AssertionError("fresh isolated Bridge unexpectedly returned a task")
    status = valid.request("GET", "/v1/status")
    if status.get("state") != "online":
        raise AssertionError("status endpoint did not report online")

    disabled = json_request(
        opener,
        f"{args.base_url}/api/plugin/off",
        {"name": "astrbot_plugin_feiyu_bridge"},
    )
    if disabled.get("status") != "ok":
        raise AssertionError(f"isolated plugin disable failed: {disabled}")
    try:
        valid.request("GET", "/v1/status")
    except BridgeHttpError as exc:
        if exc.status != 200 or exc.payload.get("status") != "error":
            raise AssertionError(
                f"unexpected response after plugin disable: {exc.payload}"
            ) from exc
    else:
        raise AssertionError("Bridge route remained callable after plugin disable")

    # Deliberately avoid printing either temporary API key or HMAC secret.
    print("REAL_ASTRBOT_DOUBLE_AUTH_OK")
    print("REAL_ASTRBOT_PLUGIN_ROUTES_OK")
    print("REAL_ASTRBOT_ROUTE_CLEANUP_OK")


if __name__ == "__main__":
    main()

