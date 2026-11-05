# Feiyu Autonomous Task: phase-1 design and isolation status

This document describes source code only. The feature is disabled by default,
has not been installed on the VPS, and cannot execute against Windows DSH
v0.2.0 until the Windows client implements and advertises the two capabilities
defined below.

## Findings from proactive-chat v1.2.6

The review baseline is the stable v1.2.6 `635fc18` release commit. Current
`main` was also checked on 2026-11-05; its refactored `LlmService` and Provider
adapter preserve the same call semantics.

1. `core/llm_adapter.py` resolves a Provider, builds `ProviderRequest`, manually
   dispatches the standard waiting/request/response hooks, and finally calls
   `provider.text_chat(...)` directly. Current `main` moves this into
   `core/services/llm_service.py` and `core/adapters/astrbot_provider.py`, whose
   terminal call is still `provider.text_chat(...)`. It does not enter
   AstrBot's Agent tool execution loop. Consequently, an LLM response generated
   by proactive-chat cannot be expected to execute `dsh_execute`.
2. `core/task_scheduler.py` uses APScheduler date jobs plus asyncio timers. Jobs
   call `check_and_chat(session_id)`. State such as the next trigger time and
   unanswered count is persisted per UMO. Quiet hours are checked again in the
   core flow before generation.
3. `core/proactive_event.py` creates `ProactiveMessageEvent`, whose message ID
   starts with `proactive:` and whose `raw_message` is `None`. The plugin
   manually dispatches AstrBot's standard LLM, decorating and
   `OnAfterMessageSentEvent` hooks. There is no documented, plugin-owned public
   autonomous-task hook or cross-plugin service API.
4. Cooperation therefore uses only AstrBot's standard `after_message_sent`
   hook as an optional timing hint. The Bridge checks the proactive event
   signature without importing proactive-chat internals. Content and sender
   identity are not consumed. A forged or changed hint can at most request a
   rate-gated check; it cannot grant a capability.
5. The Bridge also has its own persistent periodic check. Its quiet-hours and
   minimum-frequency settings should be configured to match proactive-chat.
   A proactive hint that arrives after proactive-chat's own quiet/frequency
   checks still passes all Bridge gates again.

## Identity and permission split

```text
real QQ message -> dsh_execute -> sender allowlist -> dsh.workflow.run

schedule/proactive hint -> AutonomousTaskManager -> closed model decision
                        -> dsh.feiyu_home.diary.append/read_recent
```

The two paths do not share an authorization predicate:

- `dsh_execute` is unchanged. It still requires a real platform-source event,
  original message ID/raw payload, real UMO, and an allowlisted QQ sender.
- autonomous tasks never call `dsh_execute`, never create a synthetic QQ event,
  never use an owner QQ ID, and never receive a `session_key`.
- the autonomous origin is `autonomous:<trigger_source>` and its idempotency key
  is an independently generated run UUID.
- the autonomous model receives no tools. It returns one strict JSON decision:
  `none`, `read`, or `write`. Extra fields, paths and unsupported actions are
  rejected.
- ordinary group messages are not copied into the phase-1 decision prompt.
  A diary read is explicitly delimited as untrusted data; even a malicious
  diary entry cannot alter the server-side action or argument schema.

## Bridge-side files

- `bridge/autonomous.py`: scheduler, decision parser, limits, retry/backoff,
  private read cache, state persistence, result validation and audit logging.
- `bridge/protocol.py`: exact argument/result schemas for the two new diary
  capabilities.
- `bridge/service.py`: dispatches capability-specific validation before queueing
  and before accepting a successful result.
- `main.py`: constructs the disabled-by-default manager, resolves an explicitly
  configured autonomous Provider, and listens for the standard proactive-chat
  post-send timing hint.
- `_conf_schema.json`: enable switch, Provider ID, quiet hours, frequency, daily
  limits, timeout and retry settings.
- `tests/test_autonomous.py`: isolated fake-Agent tests. No real Windows files,
  QQ messages, Provider calls or production credentials are used.

Runtime state is stored in the Bridge plugin data directory:

- `autonomous_state.json`: counters, last attempt, next eligible time and
  consecutive failure count.
- `autonomous_audit.jsonl`: `run_id`, nullable DSH `task_id`, trigger source,
  action, status/error, result digest and reported file side effects. Diary
  contents are intentionally not written to the audit log.

Autonomous results are never passed to `event.send`, `context.send_message`, or
the proactive-chat sender pipeline.

## Windows DSH interface required (not implemented here)

DSH must add exact handlers. It must not route these tasks through the generic
workflow/LLM handler.

### `dsh.feiyu_home.diary.append`

Request arguments (exact fields):

```json
{
  "entry_id": "12345678-1234-1234-1234-123456789abc",
  "content": "one UTF-8 diary entry",
  "created_at": "2026-11-05T12:30:00+01:00"
}
```

Success result (exact fields):

```json
{
  "entry_id": "12345678-1234-1234-1234-123456789abc",
  "relative_path": "diary/2026-11-05.md",
  "bytes_written": 42,
  "sha256": "64-lowercase-hex-characters"
}
```

Required Windows behavior:

- Resolve Feiyu Home only from trusted local DSH configuration. Never accept a
  root, path, filename, glob or URI from the Bridge/model.
- Append only to the fixed logical `diary/YYYY-MM-DD.md` layout under that
  root. Canonicalize the root and target; reject reparse points, symlinks,
  junctions, UNC paths, alternate data streams and any target that escapes the
  canonical root.
- Treat `entry_id` as the idempotency key. A retry must not duplicate content.
- Enforce UTF-8, the Bridge's 8192-byte input maximum, a local file-size quota
  and bounded output metadata. Do not invoke Shell, PowerShell, cmd, a generic
  workflow, browser automation, or any subprocess.

### `dsh.feiyu_home.diary.read_recent`

Request arguments:

```json
{"limit": 3}
```

Success result:

```json
{
  "entries": [
    {
      "entry_id": "12345678-1234-1234-1234-123456789abc",
      "relative_path": "diary/2026-11-05.md",
      "created_at": "2026-11-05T12:30:00+01:00",
      "content": "one entry"
    }
  ]
}
```

The handler must enumerate only its fixed diary storage, apply the same
canonical-root and reparse-point defenses, return at most `limit` entries and
at most 32768 UTF-8 bytes total, and never read arbitrary files.

After implementation, DSH may advertise these exact names in heartbeat. The
Bridge must independently include them in `allowed_capabilities`. With either
side missing, the task remains unavailable and is retried with backoff.

## Scheduling and failure behavior

- Default: disabled.
- Default periodic check: 180 minutes; successful/attempted decisions are also
  separated by a 360-minute minimum.
- Daily limits: two successful autonomous tasks, of which at most one is a
  write.
- Offline or missing-capability checks do not call the model or queue a task.
  They set a persisted exponential retry time (15 minutes up to 360 minutes).
- A model `none` decision is a normal audited outcome, not a failure. This
  prevents mechanical daily file creation.
- One asyncio lock serializes periodic and proactive-hint checks.
- Quiet hours support both normal (`2-6`) and cross-midnight (`23-7`) ranges.

## Security risks and controls

| Risk | Control / residual risk |
|---|---|
| Synthetic event impersonates owner | Autonomous path has no QQ identity and cannot call the user capability. Existing real-event checks remain unchanged. |
| Prompt injection from group chat | Phase 1 does not include group content. Future experience ingestion must remain untrusted data and cannot modify the action schema. |
| Model requests arbitrary path or Shell | Decision JSON has a closed action set; diary arguments contain no path/command fields; protocol recursively rejects command-shaped keys. |
| Windows handler escapes Feiyu Home | Must be enforced again by DSH canonical-path and reparse-point checks. Bridge cannot prove the Windows filesystem boundary remotely. This is the main reason existing `dsh.workflow.run` is not reused. |
| Duplicate writes after timeout/retry | `entry_id` is stable for one queued task and must be idempotent in DSH. Cross-process retry semantics require the Windows implementation. |
| Private diary leaks to QQ/logs | No send API is called; audit stores digests/metadata, not diary content. Provider still receives diary content after a model-selected read, so the chosen Provider is a data processor. |
| Proactive-chat update changes event internals | The standard hook is optional. Failure to recognize it only removes the hint; the private periodic scheduler remains. No third-party source is imported or patched. |
| Server restart loses last read | Read content is intentionally memory-only. Counters/backoff persist; the model may select `read` again later. |

## Isolation gate before deployment

1. Keep `autonomous_enabled=false` and do not add diary capabilities to the
   production Bridge allowlist.
2. Run the Python unit suite with the fake Agent.
3. Implement the Windows handlers and their own traversal/reparse/idempotency
   tests in a separate development DSH instance.
4. Run a loopback Bridge/Agent test against a disposable Feiyu Home. Verify one
   append, one later read, `none`, offline retry, duplicate `entry_id`, path
   escape rejection, daily limits and that no QQ send API is invoked.
5. Review `autonomous_audit.jsonl`, then explicitly opt in. Production remains
   out of scope for this phase.
