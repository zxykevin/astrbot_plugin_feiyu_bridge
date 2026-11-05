# Implementation changes

## v0.1.2-dev

- Aligned `dsh_execute` with the Windows `dsh.workflow.run` contract. The model
  can provide only a required `prompt` and optional integer
  `timeout_seconds`; capability, arbitrary arguments, idempotency key, and
  `session_key` are no longer model-controlled.
- Added a stable `session_key` derived from the real QQ sender and unified
  conversation origin. Every workflow lease requires this field.
- Restricted the complete capability registry to `dsh.status.read` and
  `dsh.workflow.run` and fail closed for any other configured capability.
- Added explicit rejection of cron, proactive-chat, non-platform and empty
  synthetic events before authorization or queueing.
- Made task deadlines authoritative at result/cancel/status boundaries and
  retained the single execution slot until a timed-out/cancelled task is
  acknowledged by the Agent.
- Added state-race, result-idempotency, session-isolation, tool-schema and
  synthetic-event tests.

Compatibility impact: the protocol ID and HTTP/HMAC envelope remain
`feiyu-bridge/1`. The previously unspecified example workflow object is now the
strict Windows-compatible capability schema
`{prompt, session_key, timeout_seconds?}`. Older experimental clients sending
`workflow`/`inputs` will be rejected with `INVALID_REQUEST`. The HMAC heartbeat
vector is unchanged because its wire body did not contain workflow arguments.

## v0.1.1-dev

- Added best-effort removal of this plugin's registered Web API handlers during
  `terminate()`.
- AstrBot 4.29.4 exposes `register_web_api()` but no public unregister method and
  does not automatically remove entries from `Context.registered_web_apis` when
  a plugin terminates.
- Cleanup compares route, methods and handler object identity. An older plugin
  instance therefore cannot remove a handler already replaced by a newer hot-
  reloaded instance.

Protocol impact: none. Feiyu Bridge Protocol remains `feiyu-bridge/1`; paths,
headers, payloads and error semantics are unchanged.

- Added fixed HMAC interoperability vectors, an independent mock Windows
  client, real-HTTP route tests, an isolated AstrBot 4.29.4 E2E driver, and
  Windows/E2E integration documentation.
- Documented the complete set of error codes already emitted by the phase-1
  implementation. This is a documentation correction, not a wire change.

