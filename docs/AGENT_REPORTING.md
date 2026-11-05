# Agent-based async task reporting

## Architecture audit

The implementation is pinned to AstrBot `4.29.4`, matching `metadata.yaml`.
AstrBot's own background-tool flow in `astr_agent_tool_exec.py` establishes the
supported pattern: reconstruct a `MessageSession`, create a `CronMessageEvent`,
load the current conversation with `_get_session_conv`, build the normal main
Agent with `build_main_agent`, run it, and persist a summary with
`persist_agent_history`.

This is materially different from calling `provider.text_chat()`. The main
Agent builder selects the provider for the original UMO, restores the current
conversation, applies the configured persona and normal Agent hooks, and uses
the session's runner configuration. Feiyu additionally invokes the standard
`OnLLMRequest` hook before runner reset so memory plugins that enrich that hook
can participate. The existing proactive-chat plugin is therefore not used as
the execution engine; its direct provider call cannot provide the complete
Agent lifecycle required here.

The active-message API remains `Context.send_message(original_umo,
MessageChain)`. It is used after the Agent has generated a final answer and for
the bounded fallback notification.

## Data and control flow

1. DSH terminal state and complete result are committed to SQLite.
2. The persisted notification row is claimed independently of task state.
3. The stored trusted QQ owner and original UMO are revalidated.
4. A bounded completion object is created from `task_id`, status, original
   prompt, error, timestamps, and a JSON result excerpt. The complete result
   remains in SQLite and is available through `feiyu_task_status`.
5. The original UMO's conversation, provider, persona, configuration and Agent
   hooks are restored through AstrBot's main-Agent builder.
6. The completion object is marked as untrusted tool data. After memory hooks
   have run, the report security instruction is appended again and the Agent's
   tool set is replaced with an empty `ToolSet`.
7. A compact summary is appended to that same conversation history on a bounded
   best-effort basis, then the Agent's concise answer is sent to the original
   UMO as the report call's final awaited operation.
8. Delivery is committed as `agent_sent`. If the Agent/provider times out or
   fails, a short notification containing only task ID, terminal status and a
   query hint is sent and committed as `fallback_sent`.

Task execution status and report status are deliberately independent. The
existing persisted `notification_status` column is the report state machine;
the query API also exposes it as `agent_report_status`. A report failure never
changes a terminal DSH task from `completed` to `failed`.

## Security and loop prevention

- Only tasks whose persisted `trusted_qq_id` is still in
  `authorized_sender_ids` may wake an Agent.
- The persisted `qq_session_id` must be non-empty and exactly match the task's
  original UMO. A security rejection does not send a fallback message.
- The synthetic event carries an internal `feiyu_task_completion` marker.
  Every Bridge tool rejects that marker before inspecting model-provided data.
- The reporting Agent receives no tools. This prevents DSH resubmission and
  also prevents unrelated tool side effects during automatic reporting.
- DSH output is never inserted into a system message. It is JSON-serialized,
  length-bounded, and described by the system prompt as untrusted tool data.
- Persisted claim states prevent normal polling/retries from repeating a report
  after its confirmed send has been committed. Interrupted `sending` claims
  become `retry` on Bridge restart.
- Reports for the same UMO use both a reporter-local lock and AstrBot's global
  session lock, preventing concurrent history updates with normal chat.

## Configuration

- `agent_report_enabled` (default `true`)
- `agent_report_timeout_seconds` (default `90`)
- `agent_report_max_result_chars` (default `12000`)
- `agent_report_max_output_chars` (default `2000`)
- `agent_report_max_steps` (default `4`)
- `agent_report_max_concurrency` (default `2`; same-session work is serial)
- `notification_send_timeout_seconds` (default `15`)

The existing poll, retry-count and retry-delay settings remain in force.

## Verification and limitations

The unit suite covers bounded structured events, raw-result retention,
exactly-once delivery, fallback and retry, restart recovery, owner revocation,
same-session serialization and completion-event DSH denial. It also retains all
v2 scheduler, authorization, cancellation and synchronous-compatibility tests.

The tests use an injected Agent runtime because the repository's isolated test
environment does not contain a configured QQ adapter, provider, persona or
LivingMemory database. Consequently, actual persona tone, memory retrieval and
adapter delivery require a non-production AstrBot `4.29.4` integration test.
When the original session uses a non-local Agent runner, this implementation
fails closed to the short fallback notification rather than silently losing
the session's semantics.

QQ active messaging has no delivery idempotency key. A process crash in the
narrow interval after QQ accepts a message but before SQLite commits
`agent_sent`/`fallback_sent` can therefore cause one duplicate after restart.
Preferring the opposite ordering would risk permanently losing notifications.

## Rollback

1. Stop accepting new async submissions and let claimed reports finish.
2. Back up the plugin data directory, especially `tasks.sqlite3`.
3. Restore the previous Bridge source and configuration.
4. Keep the SQLite database. No table migration was introduced; previous
   `sent` rows and the new terminal report-status strings are non-claimable.
5. Start through the operator's normal maintenance procedure and validate one
   harmless status task. Never replay `unknown` tasks automatically.

No production host, DSH plugin, container or service is modified by this
change.
