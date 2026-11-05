# Bridge Protocol v1 HMAC test vectors

The machine-readable vectors are in `tests/vectors/hmac_v1.json`. All values in
that file are public test data and must never be reused as deployment secrets.
The workflow capability and deterministic session derivation have a separate
machine-readable vector in `tests/vectors/workflow_v1.json`. The heartbeat HMAC
vector did not change in `v0.1.2-dev`.

## Fixed valid request

| Field | Value |
|---|---|
| Method | `POST` |
| Protocol path | `/v1/heartbeat` |
| Client ID | `windows-dsh-test` |
| Timestamp | `1793790000` |
| Nonce | `0123456789abcdefABCDEF_-` |
| Body SHA-256 | `7a6ee344d1ccec198bb7bbb9a66dbf2602191609affffcc2340b704ed19c9a42` |
| HMAC-SHA256 | `503f9a83b2a8fabdfa39b7bcec14e3a80d5c353d1bec69340701930f625ce2ac` |

The exact UTF-8 body is:

```json
{"active_task_id":null,"agent_version":"0.1.0-test","capabilities":["dsh.workflow.run"]}
```

The canonical message contains no trailing newline:

```text
FEIYU-BRIDGE-V1
POST
/v1/heartbeat
1793790000
0123456789abcdefABCDEF_-
7a6ee344d1ccec198bb7bbb9a66dbf2602191609affffcc2340b704ed19c9a42
```

## Negative cases

| Case | Operation | Expected result |
|---|---|---|
| Body tampering | Append `dsh.status.read` but retain the original signature | `AUTH_INVALID_SIGNATURE` |
| Expired timestamp | Verify at Unix time `1793790061` with a 60-second window | `AUTH_TIMESTAMP_EXPIRED` |
| Repeated nonce | Submit the valid request twice to the same server process | first accepted; second `AUTH_REPLAY` |

Clients must hash the exact bytes sent. Serializing the same JSON again after
signing can change whitespace or key order and invalidate the signature.

