# Integration preparation report

This report records the earlier `v0.1.1-dev` integration run. For the current
Windows protocol repair and test result, see `COMPATIBILITY_REPORT.md`.

Date: 2026-11-04

## Result

The phase-1 server is ready for Windows DSH integration testing. No production
AstrBot, NapCat, LivingMemory, proactive-chat, firewall, or SSH configuration
was modified or restarted.

## Evidence

- Standard-library suite: 33 tests passed, covering authentication, fixed HMAC
  vectors, protocol validation, queue/idempotency/timeout behavior, and HTTP
  route behavior.
- HTTP harness: real loopback HTTP requests exercised the actual
  `BridgeWebApi` handlers for heartbeat, lease, result, cancellation, and route
  removal. The harness models AstrBot's `plugin` scope gate before the handler.
- Isolated real runtime: a temporary resource-limited AstrBot 4.29.4 container
  loaded plugin `v0.1.1-dev`. Real extension requests confirmed missing key
  HTTP 401, `chat` scope HTTP 403, wrong HMAC Bridge HTTP 401, valid dual-auth
  heartbeat/status/empty lease, and route removal after plugin disable.
- Shutdown log: AstrBot invoked `terminate()` and the plugin reported six owned
  routes removed. A subsequent extension request returned AstrBot's route-not-
  found envelope.
- The temporary container, tunnel artifacts, and remote test files were removed
  after the run.

The actual AstrBot check deliberately used an empty task queue. Task result and
cancellation round trips are covered through real HTTP in the local harness;
they should be repeated with a development DSH Agent before any production
deployment.

## Route lifecycle finding

AstrBot 4.29.4 exposes `Context.register_web_api()` but no public unregister
method, and its plugin-disable flow calls `terminate()` without automatically
removing registered Web API entries. The plugin therefore performs a narrow,
best-effort cleanup against the context registry. It removes an entry only when
route, method list, and handler object identity all match the current plugin
instance. No AstrBot core source was modified.

## Compatibility

There is no Protocol v1 wire change. `v0.1.1-dev` adds lifecycle cleanup,
interoperability vectors, a mock client, black-box HTTP tests, and integration
documentation. The additional error-code inventory documents codes already
emitted by the phase-1 implementation.

