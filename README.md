# astrbot_plugin_feiyu_bridge

Feiyu Bridge is an AstrBot 4.29.4 plugin that brokers authenticated, structured
tasks between AstrBot and one Windows DSH Agent. If the Agent is offline, tool
calls fail closed while QQ, NapCat, LivingMemory and proactive chat continue.

## Security defaults

- No network listener is created by the plugin.
- HTTP routes use AstrBot's official plugin Web API registration.
- AstrBot OpenAPI authentication and Bridge HMAC are both required.
- The execution sender list and capability allowlist are empty by default.
- Generic Shell, SSH, PowerShell, cmd and process execution are permanently denied.
- Only one DSH task may be leased at once.
- `dsh_execute` exposes only `prompt` and optional `timeout_seconds`; the server
  derives a stable per-user/per-conversation `session_key`.
- Cron, proactive-chat, non-platform and empty synthetic calls are rejected.
- User execution remains limited to `dsh.status.read` and `dsh.workflow.run`.
- Optional autonomous diary capabilities are disabled by default, path-free,
  and require separate exact Windows handlers; see `docs/AUTONOMOUS_TASKS.md`.

## Files

- `main.py`: AstrBot lifecycle and tool registration.
- `_conf_schema.json`: safe-by-default plugin settings.
- `bridge/`: authentication, protocol, queue, Web API and tools.
- `docs/PROTOCOL_V1.md`: Windows Agent implementation contract.
- `docs/AUTONOMOUS_TASKS.md`: proactive-chat research, phase-1 design, Windows
  interface requirements, risks and isolation gate.
- `docs/WINDOWS_DSH_CONTRACT.md`: required Windows fields, errors and sequence.
- `docs/E2E_CHECKLIST.md`: development-to-production integration gate.
- `docs/COMPATIBILITY_REPORT.md`: Windows client compatibility repair result.
- `docs/ASYNC_TASKS_V2.md`: persistent async scheduler, interfaces, recovery,
  notification, rollback and production rollout notes.
- `docs/AGENT_REPORTING.md`: original-session Agent reporting, untrusted-result
  isolation, fallback behavior, verification limits and rollback.
- `tools/mock_windows_client.py`: independent real-HTTP mock Windows Agent.
- `tests/`: standard-library unit tests for security and queue semantics.

## Local tests

Run from the plugin directory:

```powershell
python -m unittest discover -s tests -v
```

The suite includes a loopback HTTP harness in addition to protocol-core tests.
`tools/isolated_astrbot_e2e.py` performs the final dual-auth and route-lifecycle
checks against a separate AstrBot 4.29.4 development instance. See
`docs/COMPATIBILITY_REPORT.md` for the latest result.

Run the mock Agent only with test credentials supplied through environment
variables:

```powershell
$env:FEIYU_ASTRBOT_API_KEY = '<development plugin-scope key>'
$env:FEIYU_BRIDGE_SECRET = '<development HMAC secret>'
python tools/mock_windows_client.py --once
```

## Deployment status

This repository is phase-1 source only. It has not been installed into the
running VPS and does not contain production credentials.

