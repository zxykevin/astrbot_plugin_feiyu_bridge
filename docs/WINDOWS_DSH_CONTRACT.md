# Windows DSH implementation contract

This is the implementation checklist for `feiyu-bridge/1`. The normative
protocol remains `PROTOCOL_V1.md`; this document does not introduce a new
protocol version.

The optional autonomous diary extension is specified in
`AUTONOMOUS_TASKS.md`. It requires two path-free exact handlers and must not be
implemented by forwarding to `dsh.workflow.run`.

## Connection and authentication

- Reach AstrBot through an SSH local forward. Local ports `6185` and `16185`
  are both valid; use whichever is already forwarded to VPS
  `127.0.0.1:6185`. The Windows client's current `6185 -> 6185` tunnel needs no
  change.
- Send a dedicated AstrBot API key with `plugin` scope in `X-API-Key`.
  AstrBot 4.29.4 also accepts the `ApiKey` authorization scheme. Do not use the
  dashboard JWT or a key with only `chat` scope.
- Send `X-Feiyu-Client-Id`, `X-Feiyu-Timestamp`, `X-Feiyu-Nonce`, and
  `X-Feiyu-Signature` on every Bridge request. `X-Request-Id` is strongly
  recommended for correlation.
- Generate a new 16-128 character `[A-Za-z0-9_-]` nonce for every attempt,
  including retries. Use Unix seconds and keep the Windows clock synchronized.
- Sign the uppercase method, protocol path (not the AstrBot forwarding prefix),
  timestamp, nonce, and SHA-256 of the exact body bytes. The signature is 64
  lowercase hexadecimal characters.
- Keep the OpenAPI key and HMAC secret separate and store both in Windows
  Credential Manager or an equivalent OS-protected store. Never log them.

## Required endpoint fields

| Endpoint | Request fields | Response fields used by the client |
|---|---|---|
| `POST /v1/heartbeat` | `agent_version: string`, `capabilities: string[]`, `active_task_id: uuid|null`, optional bounded `pending_task_ids` | `online`, `capabilities`, `cancel_requested`, `server_time` |
| `POST /v1/tasks/lease` | `wait_seconds: number` (server clamps to 0-25) | `task`, `server_time` |
| `GET /v1/tasks/{task_id}` | no body | `task_id`, `capability`, `state`, `created_at`, `deadline_at`, optional `error` |
| `POST /v1/tasks/{task_id}/result` | success: `status: "succeeded"`, `result`; failure: `status: "failed"`, `error` | terminal task state |
| `POST /v1/tasks/{task_id}/cancelled` | empty JSON object | `cancelled` or `timed_out` task state |
| `GET /v1/status` | no body | Bridge status and effective capabilities |

For `dsh.workflow.run`, `arguments` contains exactly:

- `prompt`: required non-empty string from the model tool call;
- `session_key`: required server-derived string; the client must reject a
  missing or invalid value;
- `timeout_seconds`: optional integer, bounded again by the client and deadline.

The model never supplies `session_key`, capability, arbitrary arguments, or an
idempotency key. AstrBot derives a stable key from both the real QQ sender ID
and the unified conversation origin. Different users in one group and one user
in different conversations therefore receive distinct Agent contexts.

Every response must first be checked for the success envelope
`ok=true, protocol="feiyu-bridge/1", data=...`. On failure, read
`error.code`, `error.message`, and `error.retryable`. Workflow success may also
return `dsh_session_id` and nullable `execution_id` for persisted correlation. Unknown fields must be
ignored for forward compatibility; missing required fields must fail closed.

For a leased task, validate:

- `task_id` is a UUID and matches all later status/result URLs;
- `state` equals `leased` and `deadline_at` is still in the future;
- `capability` is present in a local exact capability-to-handler map;
- `arguments` is a JSON object accepted by that handler's own schema.

There must be no generic fallback, command-line concatenation, Shell,
PowerShell, SSH, `cmd.exe`, arbitrary executable, or arbitrary script handler.
Results must be JSON-safe and remain below the configured payload limit.

## Error handling

| Code | Retry | Client action |
|---|---:|---|
| `BRIDGE_NOT_CONFIGURED` | no | Stop; server configuration is incomplete. |
| `BRIDGE_SHUTTING_DOWN` | yes | Stop leasing and reconnect with backoff. |
| `AUTH_CLIENT_REJECTED` | no | Stop; client ID is wrong. |
| `AUTH_INVALID_NONCE` | no | Fix nonce generator. |
| `AUTH_INVALID_TIMESTAMP` | no | Fix timestamp encoding. |
| `AUTH_TIMESTAMP_EXPIRED` | yes | Synchronize the clock, generate a fresh nonce, and retry. |
| `AUTH_INVALID_SIGNATURE` | no | Check canonical bytes/path/secret; do not retry unchanged. |
| `AUTH_REPLAY` | no | Generate a fresh nonce; investigate duplicate transmission. |
| `BODY_OFFLINE` | yes | Agent is considered offline; resume heartbeats. |
| `CAPABILITY_DENIED` | no | Capability is not permitted by the server. |
| `CAPABILITY_UNAVAILABLE` | yes | Advertise only implemented capabilities; retry after heartbeat. |
| `QUEUE_FULL` | yes | Apply bounded exponential backoff. |
| `IDEMPOTENCY_CONFLICT` | no | Never reuse a tool idempotency key for different input. |
| `INVALID_REQUEST`, `INVALID_TASK_ID`, `INVALID_RESULT` | no | Treat as an implementation defect. |
| `TASK_NOT_FOUND` | no | Discard stale local task state. |
| `TASK_NOT_LEASED` | no | Do not submit a result before lease. |
| `TASK_NOT_CANCELLED` | no | Do not acknowledge a task that was not cancelled. |
| `TASK_ALREADY_TERMINAL` | no | Stop unless retrying the byte-equivalent terminal result. |
| `TASK_TIMEOUT` | yes | Stop local work and acknowledge cancellation if still active. |
| `TASK_CANCELLED` | no | Stop local work and acknowledge cancellation. |
| `PAYLOAD_TOO_LARGE` | no | Reduce request/result size; do not transfer files through Bridge. |
| `INTERNAL_ERROR` | yes | Back off and preserve only safe diagnostic metadata. |

AstrBot authentication failures occur before the Bridge envelope: missing or
invalid OpenAPI key returns HTTP 401; insufficient scope returns HTTP 403. Do
not mistake these for HMAC failures.

## Call sequence

1. Establish and monitor the SSH local forward.
2. Send an immediate heartbeat with the exact locally implemented capability
   list and `active_task_id=null`.
3. Long-poll `tasks/lease`; only one task may execute at a time.
4. Validate the lease and dispatch through the exact handler map.
5. Continue heartbeats every 5-10 seconds with the active task ID.
6. If heartbeat returns `cancel_requested=true`, signal cooperative stop,
   release local resources, then call `/cancelled`.
7. Otherwise submit one success or failure result. Retrying the exact same
   terminal payload is idempotent; a conflicting terminal payload is rejected.
8. Clear the active task locally and resume leasing.
9. On tunnel loss or repeated transport failure, stop taking new work, cancel
   local work safely, reconnect with bounded jittered backoff, and heartbeat
   before leasing again.

## Creating the AstrBot API key

In AstrBot 4.29.4, create a dedicated key in the dashboard API-key page and
select only the `plugin` scope. The equivalent authenticated APIs are:

- `POST /api/v1/api-keys`, requiring a dashboard JWT or existing `system`
  scope; or
- legacy dashboard endpoint `POST /api/apikey/create`, requiring a dashboard
  JWT.

The JSON creation body is
`{"name":"feiyu-bridge","scopes":["plugin"],"expires_in_days":<days>}`.
The raw key is returned only at creation time. Store it immediately in Windows
Credential Manager and send it as `X-API-Key`; do not place it in plugin config,
logs, source control, or screenshots. The current implementation uses an
`abk_` prefix, but clients must treat the key as opaque.

