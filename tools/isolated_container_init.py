"""Initialize an ephemeral AstrBot data layer from environment-only secrets."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tarfile
from pathlib import Path


DATA = Path("/AstrBot/data")


def _dashboard_config(password: str) -> dict:
    salt = secrets.token_hex(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
    ).hex()
    return {
        "dashboard": {
            "enable": True,
            "username": "astrbot",
            "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
            "pbkdf2_password": f"pbkdf2_sha256${iterations}${salt}${digest}",
            "password_storage_upgraded": True,
            "password_change_required": False,
            "jwt_secret": secrets.token_hex(32),
            "host": "0.0.0.0",
            "port": 6185,
            "disable_access_log": True,
        }
    }


def main() -> None:
    bridge_secret = os.environ["FEIYU_E2E_BRIDGE_SECRET"]
    control_secret = os.environ["FEIYU_E2E_CONTROL_SECRET"]
    dashboard_password = os.environ["FEIYU_E2E_DASHBOARD_PASSWORD"]

    plugin_dir = DATA / "plugins" / "astrbot_plugin_feiyu_bridge"
    driver_dir = DATA / "plugins" / "astrbot_plugin_feiyu_bridge_e2e_driver"
    config_dir = DATA / "config"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    driver_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open("/tmp/server.tar.gz", "r:gz") as archive:
        archive.extractall(plugin_dir, filter="data")
    with tarfile.open("/tmp/driver.tar.gz", "r:gz") as archive:
        archive.extractall(driver_dir, filter="data")

    (DATA / "cmd_config.json").write_text(
        json.dumps(_dashboard_config(dashboard_password)), encoding="utf-8"
    )
    (config_dir / "astrbot_plugin_feiyu_bridge_config.json").write_text(
        json.dumps(
            {
                "bridge_secret": bridge_secret,
                "client_id": "windows-dsh-e2e",
                "authorized_sender_ids": ["e2e-test-user"],
                "allowed_capabilities": [
                    "dsh.status.read",
                    "dsh.workflow.run",
                ],
                "heartbeat_timeout_seconds": 25,
                "request_timeout_seconds": 30,
                "max_request_timeout_seconds": 120,
                "max_queue_size": 8,
                "max_payload_bytes": 262144,
                "auth_clock_skew_seconds": 60,
                "task_retention_seconds": 300,
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "astrbot_plugin_feiyu_bridge_e2e_driver_config.json").write_text(
        json.dumps({"control_secret": control_secret}), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
