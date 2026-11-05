# Windows DSH compatibility report

Date: 2026-11-04  
Server build: `v0.1.2-dev`  
Protocol: `feiyu-bridge/1`

## Compatibility decision

The server is compatible with the Windows client's strict
`dsh.workflow.run` contract. The HTTP paths, envelopes, HMAC canonicalization,
heartbeat vector and protocol identifier are unchanged.

The prior phase-1 lease example used an unspecified `workflow` object. It is
now formally replaced by the Windows-compatible capability arguments:

```json
{
  "prompt": "required string",
  "session_key": "server-derived string",
  "timeout_seconds": 60
}
```

This is a Protocol v1 capability-schema correction. An experimental client
that still sends `workflow` or `inputs` is intentionally rejected with
`INVALID_REQUEST`; the completed Windows client already implements the new
shape. The fixed heartbeat HMAC vector remains valid. A separate fixed workflow
and session-derivation vector is available at
`tests/vectors/workflow_v1.json`.

## P0 findings

- The model-visible tool accepts only `prompt` and optional integer
  `timeout_seconds`. Capability, argument object, idempotency key and session
  identity are not model-controlled.
- A 63-character `qq:<sha256-prefix>` session key is deterministically derived
  from both the verified event sender and AstrBot unified conversation origin.
  It is stable within one user/conversation and distinct across either boundary.
- Workflow validation requires the trusted session key before a task can be
  queued or leased.
- The complete capability registry is exactly `dsh.status.read` and
  `dsh.workflow.run`; safe-looking additional capabilities are rejected, and
  command/system capabilities remain permanently denied.
- Authorization fails closed unless the context is an original AstrBot
  platform-source event with original message metadata. Known cron and
  proactive-chat pseudo events, non-platform events, empty contexts and
  unauthorized senders are rejected before queueing.

## P1 findings

- Deadlines are evaluated before result, cancel acknowledgement, heartbeat and
  task-status transitions. A result arriving after the deadline cannot win.
- Cancel/timeout retains the single leased slot until `/cancelled` is
  acknowledged, preventing overlapping Windows executions.
- Result-versus-cancel races are deterministic under the asyncio state machine:
  the first terminal transition wins. A late conflicting result returns
  `TASK_ALREADY_TERMINAL`.
- Repeating a semantically identical result is safe. Canonical JSON hashing
  makes object-key order irrelevant; a different terminal result is rejected.
- Local SSH port `6185` is supported. `16185` is only an optional local
  collision-avoidance example, not a protocol requirement.
- AstrBot 4.29.4 can create the key in its dashboard API-key page, through
  `POST /api/v1/api-keys` with dashboard/system authentication, or through the
  legacy dashboard endpoint `POST /api/apikey/create`. Use only `plugin` scope
  and send the resulting opaque key as `X-API-Key`.

## Safety and deployment

The server suite contains 52 passing tests. Coverage includes the fixed HMAC
and workflow vectors, explicit tool schema, session isolation, synthetic-event
denial, capability registry, result retry, cancellation acknowledgement,
deadline/result races, loopback HTTP routes, OpenAPI scope behavior and route
cleanup. An in-memory compatibility probe loaded the updated modules inside the
existing AstrBot 4.29.4 runtime and returned `ASTRBOT_RUNTIME_PROBE_OK`; it did
not register the development plugin or change runtime state.

The Windows client suite was also rerun unchanged after the server repair: all
51 tests passed, including isolated sessions, cancellation, deadline handling,
`X-API-Key` authentication and the fixed HMAC interoperability vector.

No production AstrBot, NapCat, LivingMemory, proactive-chat or SSH
configuration was modified. This build has not been deployed to production.
