import unittest
import json
from pathlib import Path

from bridge.protocol import (
    AUTONOMOUS_DIARY_APPEND,
    AUTONOMOUS_DIARY_READ,
    validate_arguments,
    validate_capability_arguments,
    validate_capability,
    validate_idempotency_key,
    validate_workflow_arguments,
)


class ProtocolValidationTests(unittest.TestCase):
    def test_safe_capability(self):
        self.assertEqual(validate_capability("DSH.Workflow.Run"), "dsh.workflow.run")

    def test_command_capabilities_are_permanently_denied(self):
        for value in ("shell.exec", "ssh.connect", "dsh.powershell.run", "cmd.run"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_capability(value)

    def test_idempotency_key(self):
        self.assertEqual(
            validate_idempotency_key("request:12345678"), "request:12345678"
        )

    def test_command_shaped_arguments_are_denied_recursively(self):
        with self.assertRaises(ValueError):
            validate_arguments({"workflow": {"command": "whoami"}})

    def test_structured_arguments_are_allowed(self):
        validate_arguments({"workflow": "status", "inputs": ["cpu", "memory"]})

    def test_workflow_arguments_match_windows_schema(self):
        value = {
            "prompt": "summarize this request",
            "session_key": "qq:abc123",
            "timeout_seconds": 30,
        }
        self.assertIs(validate_workflow_arguments(value), value)

    def test_workflow_arguments_reject_unknown_or_missing_session(self):
        for value in (
            {"prompt": "hello"},
            {"prompt": "hello", "session_key": "qq:1", "workflow": "legacy"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_workflow_arguments(value)

    def test_autonomous_diary_append_is_path_free_and_bounded(self):
        value = {
            "entry_id": "12345678-1234-1234-1234-123456789abc",
            "content": "一条隔离测试日记",
            "created_at": "2026-11-05T12:30:00+01:00",
        }
        self.assertIs(
            validate_capability_arguments(AUTONOMOUS_DIARY_APPEND, value), value
        )
        with self.assertRaises(ValueError):
            validate_capability_arguments(
                AUTONOMOUS_DIARY_APPEND, {**value, "path": "C:/private.txt"}
            )

    def test_autonomous_diary_read_only_accepts_small_limit(self):
        value = {"limit": 3}
        self.assertIs(
            validate_capability_arguments(AUTONOMOUS_DIARY_READ, value), value
        )
        with self.assertRaises(ValueError):
            validate_capability_arguments(AUTONOMOUS_DIARY_READ, {"limit": 100})

    def test_fixed_workflow_vector(self):
        path = Path(__file__).parent / "vectors" / "workflow_v1.json"
        vector = json.loads(path.read_text(encoding="utf-8"))
        validate_workflow_arguments(vector["valid_arguments"])
        for case in vector["invalid_cases"]:
            with self.subTest(name=case["name"]), self.assertRaises(ValueError):
                validate_workflow_arguments(case["arguments"])


if __name__ == "__main__":
    unittest.main()

