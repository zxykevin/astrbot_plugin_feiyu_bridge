"""Emit a read-only in-memory compatibility probe for an AstrBot container.

Usage:
    python tools/emit_runtime_probe.py | ssh HOST \
        'sudo docker exec -i dafeiyu-astrbot python -'

The remote process does not write plugin files or register routes/tools.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "feiyu_probe.bridge.protocol": ROOT / "bridge" / "protocol.py",
    "feiyu_probe.bridge.auth": ROOT / "bridge" / "auth.py",
    "feiyu_probe.bridge.service": ROOT / "bridge" / "service.py",
    "feiyu_probe.bridge": ROOT / "bridge" / "__init__.py",
    "feiyu_probe.bridge.tools": ROOT / "bridge" / "tools.py",
    "feiyu_probe.bridge.web": ROOT / "bridge" / "web.py",
    "feiyu_probe.main": ROOT / "main.py",
}

payload = {
    name: base64.b64encode(path.read_bytes()).decode("ascii")
    for name, path in FILES.items()
}

print(
    """import base64, json, sys, types
payload = json.loads(%r)
for package in ("feiyu_probe", "feiyu_probe.bridge"):
    module = types.ModuleType(package)
    module.__path__ = []
    sys.modules[package] = module
for name in (
    "feiyu_probe.bridge.protocol",
    "feiyu_probe.bridge.auth",
    "feiyu_probe.bridge.service",
    "feiyu_probe.bridge",
    "feiyu_probe.bridge.tools",
    "feiyu_probe.bridge.web",
    "feiyu_probe.main",
):
    module = sys.modules.get(name) or types.ModuleType(name)
    module.__package__ = name if name == "feiyu_probe.bridge" else name.rpartition(".")[0]
    if name == "feiyu_probe.bridge":
        module.__path__ = []
    sys.modules[name] = module
    exec(compile(base64.b64decode(payload[name]), name, "exec"), module.__dict__)
service_module = sys.modules["feiyu_probe.bridge.service"]
tools_module = sys.modules["feiyu_probe.bridge.tools"]
service = service_module.BridgeService(
    client_id="probe",
    allowed_capabilities={"dsh.workflow.run"},
)
status_tool = tools_module.GetBodyStatusTool(service=service)
execute_tool = tools_module.DshExecuteTool(
    service=service,
    authorized_sender_ids=frozenset({"probe"}),
)
assert status_tool.name == "get_body_status"
assert execute_tool.name == "dsh_execute"
assert execute_tool.parameters["type"] == "object"
assert set(execute_tool.parameters["properties"]) == {"prompt", "timeout_seconds"}
assert execute_tool.parameters["required"] == ["prompt"]
assert "session_key" not in execute_tool.parameters["properties"]
assert sys.modules["feiyu_probe.main"].FeiyuBridgePlugin.__name__ == "FeiyuBridgePlugin"
print("ASTRBOT_RUNTIME_PROBE_OK")
"""
    % json.dumps(payload, separators=(",", ":"))
)

