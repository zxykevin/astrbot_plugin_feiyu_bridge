# End-to-end integration checklist

## Isolated AstrBot preparation

- [ ] Use AstrBot 4.29.4 with a new data directory; do not mount production
      AstrBot, NapCat, LivingMemory, or SSH configuration.
- [ ] Bind the development WebUI only to loopback and reach it through an SSH
      local forward.
- [ ] Install the plugin under `astrbot_plugin_feiyu_bridge` and confirm the log
      reports the expected plugin version.
- [ ] Configure a test-only 32+ character HMAC secret, unique client ID, and
      minimal capability allowlist.
- [ ] Create a short-lived API key with only `plugin` scope and a comparison key
      with only `chat` scope. Do not print either key.

## Authentication and HMAC

- [ ] Run `tests/test_vectors.py` against the fixed vectors.
- [ ] Confirm a missing OpenAPI key returns HTTP 401 before Bridge processing.
- [ ] Confirm the `chat`-only key returns HTTP 403.
- [ ] Confirm valid `plugin` scope plus wrong HMAC returns Bridge HTTP 401 with
      `AUTH_INVALID_SIGNATURE`.
- [ ] Confirm valid dual authentication accepts heartbeat and status calls.
- [ ] Confirm body tampering, stale timestamp, malformed nonce, and nonce replay
      produce their documented errors.
- [ ] Confirm signing uses `/v1/...`, never the AstrBot forwarding prefix.

## Agent lifecycle

- [ ] Start the mock Windows client and send a heartbeat immediately.
- [ ] Confirm server and client intersect capability allowlists.
- [ ] Confirm user execution contains only `dsh.status.read` and
      `dsh.workflow.run`; autonomous diary capabilities remain disabled until
      the separate `AUTONOMOUS_TASKS.md` isolation gate passes.
- [ ] Confirm every workflow lease contains `prompt`, server-derived
      `session_key`, and bounded `timeout_seconds`; reject unknown keys.
- [ ] Confirm two QQ users in one group and one user in two conversations get
      different session keys, while repeated turns in one conversation are stable.
- [ ] Confirm cron, proactive-chat and empty synthetic events cannot queue work,
      even when their remembered sender ID is authorized.
- [ ] Queue one allowlisted task and confirm one lease only.
- [ ] Submit a successful result and a structured failed result.
- [ ] Retry the byte-equivalent terminal result and confirm idempotent success.
- [ ] Submit a conflicting terminal result and confirm rejection.
- [ ] Cancel an active task, observe `cancel_requested` on heartbeat, stop local
      work, and acknowledge `/cancelled`.
- [ ] Exercise result-vs-cancel and result-vs-timeout races in both orderings;
      exactly the first valid terminal transition must win.
- [ ] Confirm a timed-out leased task keeps the single execution slot until the
      Windows Agent acknowledges cancellation.
- [ ] Verify timeout, queue-full, offline, disallowed capability, command-shaped
      arguments, oversized payload, and duplicate idempotency-key behavior.
- [ ] Drop the SSH tunnel and confirm AstrBot, QQ/NapCat, LivingMemory, and
      proactive chat remain independent; only DSH execution degrades offline.

## Reload and shutdown

- [ ] Disable the plugin through AstrBot and confirm `terminate()` cancels
      pending work and removes exactly its six registered routes.
- [ ] Confirm another plugin's registered routes are not removed.
- [ ] Confirm old routes return AstrBot's route-not-found response after disable.
- [ ] Re-enable/reload once and confirm there is only one handler per Bridge
      route and no duplicate LLM tools.
- [ ] Stop the mock client during a task and verify server timeout recovery.

## Windows DSH readiness

- [ ] Store the API key and HMAC secret in OS-protected storage.
- [ ] Use cryptographically random nonce generation and synchronized system time.
- [ ] Implement an exact capability-handler map and per-capability JSON schema.
- [ ] Bound HTTP timeouts, result size, logs, retries, and reconnect backoff.
- [ ] Redact credentials and sensitive task arguments from logs and crash dumps.
- [ ] Record the tested plugin version, Agent build, vector version, and results.

## Production gate

- [ ] Review all isolated test evidence and unresolved risks.
- [ ] Generate new production credentials; never promote test credentials.
- [ ] Back up only the affected AstrBot plugin/config files using the deployment
      runbook; do not alter NapCat, LivingMemory, proactive chat, or firewall.
- [ ] Obtain an explicit deployment decision before changing production.
- [ ] Prepare rollback by disabling the Bridge plugin and restoring only its
      own prior files/configuration.

