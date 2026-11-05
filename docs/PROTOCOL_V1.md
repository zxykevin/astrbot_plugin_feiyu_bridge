# Feiyu Bridge Protocol v1

Protocol identifier: `feiyu-bridge/1`

This protocol connects exactly one trusted Windows DSH Agent to the AstrBot
plugin. It is a structured capability protocol, not a remote shell protocol.

## Transport

The plugin uses AstrBot 4.29.4's official `context.register_web_api()` API. The
external route is:

```text
http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_feiyu_bridge/v1/...
```

Every request has two authentication layers:

1. AstrBot OpenAPI Key with the `plugin` scope, sent as `Authorization: Bearer
   abk_...` or `X-API-Key`.
2. Feiyu Bridge HMAC headers described below.

Keep port 6185 bound to `127.0.0.1`. The Windows client should reach it through
an SSH local forward. The local port is a deployment choice; both examples are
Protocol v1 compatible:

```powershell
ssh -N -L 16185:127.0.0.1:6185 codex@VPS_HOST
ssh -N -L 6185:127.0.0.1:6185 codex@VPS_HOST
```

The client calls the matching loopback port. Port `16185` only avoids a local
collision; an existing `6185 -> 6185` tunnel does not need to change. Do not
open the remote 6185 or a new Bridge port in UFW for phase 1.

The AstrBot `plugin` scope also permits plugin-management APIs. Therefore this
OpenAPI Key must be dedicated to the Bridge, stored in the Windows credential
store, and usable only through the SSH tunnel. The Bridge HMAC secret is a
separate credential and must never equal the OpenAPI Key or SSH secret.

## HMAC authentication

Required headers:

```text
X-Feiyu-Client-Id: windows-dsh
X-Feiyu-Timestamp: Unix seconds
X-Feiyu-Nonce: at least 16 random URL-safe characters
X-Feiyu-Signature: lowercase hex HMAC-SHA256
X-Request-Id: optional caller correlation ID
```

The canonical message is UTF-8:

```text
FEIYU-BRIDGE-V1
<UPPERCASE_METHOD>
<PROTOCOL_PATH>
<TIMESTAMP>
<NONCE>
<LOWERCASE_SHA256_OF_EXACT_BODY_BYTES>
```

`PROTOCOL_PATH` excludes AstrBot's forwarding prefix. Examples are
`/v1/heartbeat` and `/v1/tasks/<uuid>/result`.

The signature is:

```text
hex(HMAC-SHA256(bridge_secret, canonical_message))
```

The server rejects expired timestamps, reused nonces, wrong client IDs and body
tampering. JSON should be encoded as UTF-8; the signature must cover the exact
bytes sent on the wire.

## Envelope

Success:

```json
{
  "ok": true,
  "protocol": "feiyu-bridge/1",
  "request_id": "optional",
  "data": {}
}
```

Failure:

```json
{
  "ok": false,
  "protocol": "feiyu-bridge/1",
  "error": {
    "code": "ERROR_CODE",
    "message": "safe message",
    "retryable": false
  }
}
```

## Endpoints

### `POST /v1/heartbeat`

Call every 5-10 seconds, including while executing a task.

```json
{
  "agent_version": "0.1.0",
  "capabilities": ["dsh.status.read", "dsh.workflow.run"],
  "active_task_id": null
}
```

The server intersects advertised capabilities with its own configuration. If
`active_task_id` has been cancelled or timed out, `cancel_requested` is true.
Version 0.4 DSH clients may also send up to 50 `pending_task_ids` so a result
completed during a Bridge restart can be re-associated before delivery retry.

### `POST /v1/tasks/lease`

```json
{"wait_seconds": 20}
```

Returns one task or `null`. Only one task can be leased at a time.

```json
{
  "task": {
    "task_id": "uuid",
    "capability": "dsh.workflow.run",
    "arguments": {
      "prompt": "Summarize the current request",
      "session_key": "qq:8b96a9f6e80d064af0ebb7b55e541503974ab5594ff6e919f21a55754097",
      "timeout_seconds": 60
    },
    "state": "leased",
    "created_at": 0,
    "deadline_at": 0
  },
  "server_time": 0
}
```

The Agent must dispatch by an exact capability-to-handler map. Never concatenate
arguments into a command line and never implement a generic fallback handler.

`dsh.workflow.run` accepts exactly `prompt` (required non-empty string),
`session_key` (required `[A-Za-z0-9._:@-]{1,64}`), and `timeout_seconds`
(optional integer). AstrBot exposes only `prompt` and optional
`timeout_seconds` to the model. It derives `session_key` from the authenticated
QQ sender and AstrBot conversation origin, so the model cannot select or
collide Agent contexts. The lease must always contain this trusted key.

### `GET /v1/tasks/<task_id>`

Returns the task state. A running client may poll this in addition to heartbeats
when it needs faster cancellation.

### `POST /v1/tasks/<task_id>/result`

Success:

```json
{"status": "succeeded", "result": {"value": "..."}}
```

Failure:

```json
{
  "status": "failed",
  "error": {
    "code": "DSH_WORKFLOW_FAILED",
    "message": "safe error text",
    "retryable": false
  }
}
```

Repeating the semantically identical result body is accepted (object key order
does not matter because the server hashes canonical JSON). A conflicting second
terminal result is rejected.

### `POST /v1/tasks/<task_id>/cancelled`

Acknowledges that the client stopped work after receiving a cancellation. This
endpoint is idempotent.

### `GET /v1/status`

Returns online status, heartbeat age, effective capabilities and queue state.
It is authenticated even though it is read-only.

## Task states

```text
queued -> leased -> succeeded
                 -> failed
                 -> cancelled
                 -> timed_out
```

An offline Agent causes `BODY_OFFLINE` before a task is queued. AstrBot, NapCat,
LivingMemory and proactive chat continue running normally.

## Error codes

| Code | Retry | Meaning |
|---|---:|---|
| `BRIDGE_NOT_CONFIGURED` | no | HMAC secret/client ID is missing |
| `BRIDGE_SHUTTING_DOWN` | yes | Plugin termination is in progress |
| `AUTH_CLIENT_REJECTED` | no | Client ID is not allowed |
| `AUTH_INVALID_NONCE` | no | Nonce format is invalid |
| `AUTH_INVALID_TIMESTAMP` | no | Timestamp is not an integer |
| `AUTH_INVALID_SIGNATURE` | no | HMAC verification failed |
| `AUTH_TIMESTAMP_EXPIRED` | yes | Clocks differ or request is stale |
| `AUTH_REPLAY` | no | Nonce was already used |
| `BODY_OFFLINE` | yes | Heartbeat lease expired |
| `CAPABILITY_DENIED` | no | Capability is not server-allowlisted |
| `CAPABILITY_UNAVAILABLE` | yes | Agent did not advertise capability |
| `QUEUE_FULL` | yes | Pending queue reached its limit |
| `IDEMPOTENCY_CONFLICT` | no | The same key was reused with different input |
| `INVALID_REQUEST` | no | Request JSON or required field is invalid |
| `INVALID_TASK_ID` | no | Task ID is not a UUID |
| `INVALID_RESULT` | no | Result status is not `succeeded` or `failed` |
| `TASK_NOT_FOUND` | no | Unknown or expired task ID |
| `TASK_NOT_LEASED` | no | Result arrived before lease |
| `TASK_NOT_CANCELLED` | no | Cancellation acknowledgement is not applicable |
| `TASK_ALREADY_TERMINAL` | no | A conflicting terminal result was submitted |
| `TASK_TIMEOUT` | yes | Deadline expired |
| `TASK_CANCELLED` | no | Server/plugin shutdown cancelled task |
| `PAYLOAD_TOO_LARGE` | no | Request/result exceeds configured limit |
| `INTERNAL_ERROR` | yes | Server encountered an unexpected safe failure |

The added entries above document errors already emitted by the phase-1 server;
they do not change Protocol v1 paths, payloads, or behavior.

## Capability policy

Both sides must independently allowlist exact capability names. The server has a
permanent deny rule for names containing shell, SSH, PowerShell, cmd, terminal,
subprocess or generic process execution semantics. Payload keys such as
`command`, `cmd`, `argv`, `executable`, `script`, `shell`, `ssh` and `terminal`
are also rejected recursively.

The complete Protocol v1 capability registry implemented by this Bridge source is:

```text
dsh.status.read
dsh.workflow.run
dsh.feiyu_home.diary.append
dsh.feiyu_home.diary.read_recent
```

The two diary entries are an optional, disabled-by-default phase-1 extension.
They are path-free logical operations, not general file capabilities. Existing
Windows v0.2.0 clients that do not advertise them remain compatible and cannot
receive autonomous tasks. Their exact request/result contract and required
Feiyu Home boundary enforcement are in `AUTONOMOUS_TASKS.md`.

No browser, arbitrary file, input-control, Shell, SSH, PowerShell, command, or
generic system-execution capability is registered. Adding one requires an
explicit protocol/security review.

## AstrBot tool authorization

The model-visible `dsh_execute` schema is:

```json
{
  "type": "object",
  "properties": {
    "prompt": {"type": "string", "minLength": 1},
    "timeout_seconds": {"type": "integer", "minimum": 1}
  },
  "required": ["prompt"],
  "additionalProperties": false
}
```

Before queueing, the server requires an original AstrBot platform-source event,
a non-empty original message ID/raw payload, sender and conversation identity,
and membership in `authorized_sender_ids`. Cron events, proactive-chat pseudo
events, empty/synthetic contexts, and non-authorized senders fail closed before
task creation.

## Windows client loop

1. Establish and monitor the SSH local forward.
2. Send a heartbeat immediately.
3. Long-poll `tasks/lease`.
4. Validate task ID, deadline, capability and arguments.
5. Execute through the exact DSH handler while continuing heartbeats.
6. Stop promptly when `cancel_requested` becomes true.
7. Submit a bounded, JSON-safe result.
8. Never log either credential or full sensitive task payloads.

