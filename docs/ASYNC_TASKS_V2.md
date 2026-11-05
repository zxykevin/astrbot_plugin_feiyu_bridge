# Feiyu Bridge v2 asynchronous tasks

## Architecture audit result

The original Bridge used an in-memory FIFO and exposed a synchronous
`dsh_execute` tool. The HTTP protocol already had authenticated leasing,
heartbeats, idempotent result delivery, cooperative cancellation and a single
execution slot. Those mechanisms are retained.

The v2 scheduler adds SQLite persistence around that state machine. It does not
add a public listener, a generic command capability, or another runtime
dependency. The existing OpenAPI-key plus HMAC authentication and the DSH tool
and approval restrictions remain authoritative.

DSH can persist and reopen an agent session, so completed results now include
`dsh_session_id`. DSH does not expose a restart-resumable identifier for an
individual running turn. `execution_id` is therefore stored as nullable and is
not fabricated from the Bridge task ID.

## Interfaces

All tools obtain identity from the real AstrBot platform event and require the
sender to be in `authorized_sender_ids`. A model cannot provide a QQ ID,
session ID, owner filter or conversation origin.

- `feiyu_task_submit(prompt, timeout_seconds?)`: persist and return immediately.
  It can queue while DSH is temporarily offline. Repeated calls from the same
  source message with the same arguments return the same task.
- `feiyu_task_status(task_id)`: read one task owned by the current QQ sender.
- `feiyu_task_list(status?, created_after?, created_before?, limit?)`: list only
  the current QQ sender's tasks. Times are Unix seconds.
- `feiyu_task_cancel(task_id)`: cancel an unleased queued task immediately or
  request cooperative cancellation of a running task.
- `dsh_execute(prompt, timeout_seconds?)`: unchanged synchronous compatibility
  entry point. It still requires DSH to be online and waits for its result.
- `get_body_status()`: now also requires the trusted real-user context.

Task states exposed by the v2 tools are `queued`, `running`, `completed`,
`failed`, `cancel_requested`, `cancelled`, `timed_out`, and `unknown`. The v1
DSH wire representation remains `leased` and `succeeded` where required.

## SQLite schema

The database is `tasks.sqlite3` under the AstrBot plugin data directory. The
`tasks` table stores:

- identity: `task_id`, `trusted_qq_id`, `qq_session_id`, `origin`;
- request: `capability`, `arguments_json`, `task_content`, `idempotency_key`,
  `request_digest`;
- lifecycle: `created_at`, `started_at`, `ended_at`, `deadline_at`, `status`,
  `client_id`, `recovery_pending`;
- DSH correlation: nullable `dsh_session_id` and nullable `execution_id`;
- outcome: `result_json`, `error_json`, `result_digest`;
- notification: `notify_on_terminal`, `notification_status`, attempt count,
  next attempt, last safe error and sent time;
- bookkeeping: `updated_at`.

`(origin, idempotency_key)` is unique. Owner/time, state/time, and notification
indexes support the query and retry paths. SQLite WAL is used for file-backed
databases.

## Cancellation and recovery

For a running task, `feiyu_task_cancel` persists `cancel_requested`. DSH sees
that flag in heartbeat, aborts the dispatcher, invokes the official
`agent.cancel(...)`, waits for the agent to become idle, and only then posts the
`/cancelled` acknowledgement. The Bridge changes the state to `cancelled` only
after that acknowledgement. It never kills unrelated processes or the DSH
service.

On Bridge restart, queued tasks are restored. A previously running task is held
in recovery until DSH heartbeats. If `active_task_id` matches, the execution is
re-associated. A locally completed outcome awaiting retry is also recognized
through bounded `pending_task_ids`. If the first heartbeat confirms neither, or no heartbeat
arrives during the recovery window, the task becomes `unknown` and is never
automatically re-submitted. If both Bridge and DSH restart, a running turn
cannot be recovered with the current DSH API.

## Notifications

Terminal and `unknown` transitions are committed before notification delivery.
A separate loop claims persisted pending notifications and sends a plain
`MessageChain` through AstrBot `Context.send_message` to the original
`unified_msg_origin`. It does not call an LLM or the proactive-chat plugin.
Claims and attempts are persisted; process-interrupted claims become retries on
startup. The default is three attempts with bounded linear backoff.

## Configuration additions

- `async_task_timeout_seconds`: 3600 by default.
- `async_task_max_timeout_seconds`: 86400 by default.
- `recovery_grace_seconds`: 30 by default.
- `notification_poll_seconds`: 5 by default.
- `notification_max_attempts`: 3 by default.
- `notification_retry_seconds`: 30 by default.

The DSH default `tasks.maxTimeoutMs` is 86400000. An existing explicit DSH
configuration keeps its configured value and must be reviewed during rollout.

## Known limitations

- DSH session persistence restores conversation history, not an in-flight turn.
  There is no native cross-process execution ID, so `execution_id` remains null.
- DSH pending result delivery is an in-process bounded queue (10-minute default
  TTL). If DSH itself exits or that TTL expires before Bridge returns, recovery
  cannot reconstruct the outcome and the task becomes `unknown`.
- Active-message support depends on the QQ adapter. Exhausted notification
  retries remain visible as `notification_status=failed` for manual handling.
- The scheduler intentionally retains task rows; automated history pruning is
  not included in this change.
- The source directories supplied for this work are not Git repositories.

## Development test result

On 2026-11-05, the development workspace passed:

- AstrBot Bridge: 69/69 Python tests, including eight v2 async/recovery tests;
- Windows DSH plugin: 79/79 Node tests, including the existing real loopback
  cancellation/reconnect coverage and a new timeout-over-300-seconds test;
- Python bytecode compilation and `_conf_schema.json` parsing.

All v2 tests use temporary SQLite files, mock QQ events and mock/local DSH
transports. They do not invoke shutdown, private-file deletion or another
destructive remote-computer operation.

## Rollback

1. Disable the development plugin instance so it stops accepting new work.
2. Preserve `tasks.sqlite3` (and its `-wal`/`-shm` sidecars if present) for audit.
3. Restore the previous AstrBot plugin directory and previous DSH plugin
   directory from the deployment backup.
4. Restore the prior AstrBot configuration schema/values and DSH
   `tasks.maxTimeoutMs` value.
5. Start the prior versions and verify the original heartbeat/status path with
   an empty queue. Do not translate or replay v2 `unknown` tasks.

## Production rollout (not performed)

1. Review the test report and known limitations; choose a maintenance window.
2. Stop accepting new Bridge tasks and confirm no task is `running` or
   `cancel_requested`.
3. Back up the two plugin directories, AstrBot plugin configuration, DSH
   configuration, and the plugin data directory.
4. Copy the reviewed AstrBot plugin files and DSH plugin files. Do not copy test
   databases or development secrets.
5. Confirm `authorized_sender_ids`, capability allowlists, HMAC/OpenAPI
   credentials, notification limits, recovery window, and DSH
   `tasks.maxTimeoutMs`.
6. Restart only through the operator's normal maintenance procedure.
7. Validate heartbeat, an allowlisted harmless status task, async submit/status,
   one cooperative cancellation, one notification, and restart recovery.
8. Keep rollback artifacts until the observation window is complete.

No production VPS, service, container or credential was changed while
implementing this feature.
