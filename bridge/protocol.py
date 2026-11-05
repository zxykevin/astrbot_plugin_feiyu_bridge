from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from dataclasses import dataclass
from typing import Any

PROTOCOL_ID = "feiyu-bridge/1"
PROTOCOL_VERSION = 1
AUTONOMOUS_DIARY_APPEND = "dsh.feiyu_home.diary.append"
AUTONOMOUS_DIARY_READ = "dsh.feiyu_home.diary.read_recent"
BRIDGE_CAPABILITIES = frozenset(
    {
        "dsh.status.read",
        "dsh.workflow.run",
        AUTONOMOUS_DIARY_APPEND,
        AUTONOMOUS_DIARY_READ,
    }
)

CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
DENIED_CAPABILITY_PARTS = frozenset(
    {
        "shell",
        "ssh",
        "powershell",
        "pwsh",
        "cmd",
        "terminal",
        "subprocess",
        "process.exec",
        "system.command",
    }
)
DENIED_ARGUMENT_KEYS = frozenset(
    {
        "argv",
        "cmd",
        "command",
        "executable",
        "powershell",
        "script",
        "shell",
        "ssh",
        "terminal",
    }
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_capability(capability: str) -> str:
    normalized = str(capability or "").strip().lower()
    if not CAPABILITY_RE.fullmatch(normalized):
        raise ValueError("capability must match [a-z][a-z0-9_.-]{0,63}")
    if any(part in normalized for part in DENIED_CAPABILITY_PARTS):
        raise ValueError("capability is permanently denied by bridge policy")
    return normalized


def validate_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not IDEMPOTENCY_RE.fullmatch(key):
        raise ValueError("invalid idempotency key")
    return key


def ensure_json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return value


def validate_arguments(value: Any, *, depth: int = 0) -> None:
    """Reject command-shaped payloads even when a capability is allowlisted."""
    if depth > 16:
        raise ValueError("arguments exceed maximum nesting depth")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in DENIED_ARGUMENT_KEYS:
                raise ValueError(f"argument key '{key}' is denied by bridge policy")
            validate_arguments(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_arguments(child, depth=depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("arguments contain a non-JSON value")


def validate_workflow_arguments(value: Any) -> dict[str, Any]:
    """Validate the only executable Protocol v1 workflow shape."""
    arguments = ensure_json_object(value, "arguments")
    unknown = set(arguments) - {"prompt", "session_key", "timeout_seconds"}
    if unknown:
        raise ValueError(
            f"unsupported dsh.workflow.run argument: {sorted(unknown)[0]}"
        )
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if "\x00" in prompt:
        raise ValueError("prompt must not contain NUL characters")
    session_key = arguments.get("session_key")
    if not isinstance(session_key, str) or not re.fullmatch(
        r"[A-Za-z0-9._:@-]{1,64}", session_key
    ):
        raise ValueError("session_key must match [A-Za-z0-9._:@-]{1,64}")
    timeout = arguments.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 86_400
    ):
        raise ValueError("timeout_seconds must be an integer between 1 and 86400")
    validate_arguments(arguments)
    return arguments


def validate_diary_append_arguments(value: Any) -> dict[str, Any]:
    """Validate a path-free request to append one entry to Feiyu Home."""
    arguments = ensure_json_object(value, "arguments")
    unknown = set(arguments) - {"entry_id", "content", "created_at"}
    if unknown:
        raise ValueError(f"unsupported diary append argument: {sorted(unknown)[0]}")
    try:
        import uuid

        entry_id = str(uuid.UUID(str(arguments.get("entry_id", ""))))
    except ValueError as exc:
        raise ValueError("entry_id must be a UUID") from exc
    if entry_id != str(arguments.get("entry_id", "")):
        raise ValueError("entry_id must be a canonical lowercase UUID")
    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    if "\x00" in content or len(content.encode("utf-8")) > 8192:
        raise ValueError("content must be at most 8192 UTF-8 bytes and contain no NUL")
    created_at = arguments.get("created_at")
    if not isinstance(created_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
        created_at,
    ):
        raise ValueError("created_at must be an ISO-8601 timestamp with timezone")
    validate_arguments(arguments)
    return arguments


def validate_diary_read_arguments(value: Any) -> dict[str, Any]:
    """Validate a path-free recent diary read request."""
    arguments = ensure_json_object(value, "arguments")
    unknown = set(arguments) - {"limit"}
    if unknown:
        raise ValueError(f"unsupported diary read argument: {sorted(unknown)[0]}")
    limit = arguments.get("limit", 1)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("limit must be an integer between 1 and 10")
    validate_arguments(arguments)
    return arguments


def validate_capability_arguments(capability: str, value: Any) -> dict[str, Any]:
    """Dispatch exact capability schemas; there is deliberately no fallback."""
    if capability == "dsh.workflow.run":
        return validate_workflow_arguments(value)
    if capability == AUTONOMOUS_DIARY_APPEND:
        return validate_diary_append_arguments(value)
    if capability == AUTONOMOUS_DIARY_READ:
        return validate_diary_read_arguments(value)
    validate_arguments(value)
    return ensure_json_object(value, "arguments")


def _validate_diary_relative_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "diary"
    ):
        raise ValueError("relative_path must remain under diary/")
    return str(path)


def validate_capability_result(capability: str, value: Any) -> None:
    """Validate security-relevant Agent result metadata before task completion."""
    if capability not in {AUTONOMOUS_DIARY_APPEND, AUTONOMOUS_DIARY_READ}:
        return
    result = ensure_json_object(value, "result")
    if capability == AUTONOMOUS_DIARY_APPEND:
        unknown = set(result) - {
            "entry_id",
            "relative_path",
            "bytes_written",
            "sha256",
        }
        if unknown:
            raise ValueError(f"unsupported diary append result: {sorted(unknown)[0]}")
        try:
            import uuid

            entry_id = str(uuid.UUID(str(result.get("entry_id", ""))))
        except ValueError as exc:
            raise ValueError("result entry_id must be a UUID") from exc
        if entry_id != str(result.get("entry_id", "")):
            raise ValueError("result entry_id must be canonical")
        _validate_diary_relative_path(result.get("relative_path"))
        written = result.get("bytes_written")
        if isinstance(written, bool) or not isinstance(written, int) or written < 0:
            raise ValueError("bytes_written must be a non-negative integer")
        digest = str(result.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return

    if set(result) != {"entries"}:
        raise ValueError("diary read result must contain only entries")
    entries = result.get("entries")
    if not isinstance(entries, list) or len(entries) > 10:
        raise ValueError("entries must be an array of at most 10 items")
    total_bytes = 0
    for entry in entries:
        item = ensure_json_object(entry, "entry")
        if set(item) != {"entry_id", "relative_path", "created_at", "content"}:
            raise ValueError("diary entry has unsupported or missing fields")
        _validate_diary_relative_path(item.get("relative_path"))
        content = item.get("content")
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("diary entry content must be safe text")
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > 32768:
            raise ValueError("diary read result exceeds 32768 UTF-8 bytes")


@dataclass(frozen=True, slots=True)
class ProtocolError:
    code: str
    message: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def ok(data: Any, *, request_id: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": True,
        "protocol": PROTOCOL_ID,
        "data": data,
    }
    if request_id:
        response["request_id"] = request_id
    return response


def error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "protocol": PROTOCOL_ID,
        "error": ProtocolError(code, message, retryable).as_dict(),
    }
    if request_id:
        response["request_id"] = request_id
    return response

