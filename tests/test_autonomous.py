import json
import os
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from bridge.autonomous import AutonomousPolicy, AutonomousTaskManager
from bridge.protocol import AUTONOMOUS_DIARY_APPEND, AUTONOMOUS_DIARY_READ
from bridge.service import BridgeService


class FakeAutonomousService(BridgeService):
    def __init__(self):
        super().__init__(
            client_id="windows-dsh",
            allowed_capabilities={AUTONOMOUS_DIARY_APPEND, AUTONOMOUS_DIARY_READ},
        )
        self.created = []
        self._last_heartbeat = time.time()
        self._client_capabilities = {
            AUTONOMOUS_DIARY_APPEND,
            AUTONOMOUS_DIARY_READ,
        }

    async def create_task(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(task_id=str(uuid.uuid4()), request=kwargs)

    async def wait_for_result(self, task):
        if task.request["capability"] == AUTONOMOUS_DIARY_APPEND:
            arguments = task.request["arguments"]
            return {
                "entry_id": arguments["entry_id"],
                "relative_path": "diary/2026-11-05.md",
                "bytes_written": len(arguments["content"].encode("utf-8")),
                "sha256": "a" * 64,
            }
        return {
            "entries": [
                {
                    "entry_id": str(uuid.uuid4()),
                    "relative_path": "diary/2026-11-05.md",
                    "created_at": "2026-11-05T12:00:00+01:00",
                    "content": "今天记下了一件值得记住的小事。",
                }
            ]
        }


class AutonomousTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data_dir = Path(__file__).parent / f"_autonomous_test_{uuid.uuid4().hex}"
        self.data_dir.mkdir()
        self.service = FakeAutonomousService()
        self.decisions = []
        self.prompts = []

    def tearDown(self):
        for name in ("autonomous_state.json", "autonomous_audit.jsonl"):
            path = self.data_dir / name
            if path.exists():
                path.unlink()
        os.rmdir(self.data_dir)

    async def decide(self, prompt):
        self.prompts.append(prompt)
        return self.decisions.pop(0)

    def manager(self, **policy_overrides):
        values = dict(
            enabled=True,
            provider_id="autonomous-test-provider",
            quiet_hours="",
            min_interval_minutes=5,
            daily_task_limit=3,
            daily_write_limit=1,
        )
        values.update(policy_overrides)
        return AutonomousTaskManager(
            service=self.service,
            policy=AutonomousPolicy(**values),
            decision_call=self.decide,
            data_dir=self.data_dir,
        )

    async def test_model_can_decline_without_creating_task(self):
        self.decisions = ['{"action":"none","content":"","reason":"没有值得记录的事"}']
        result = await self.manager().run_once(trigger_source="isolated_test")
        self.assertTrue(result["ok"])
        self.assertFalse(result["executed"])
        self.assertEqual(self.service.created, [])
        audit = json.loads(
            (self.data_dir / "autonomous_audit.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(audit["status"], "skipped_by_model")
        self.assertEqual(audit["trigger_source"], "isolated_test")

    async def test_write_then_later_read_uses_only_structured_diary_capabilities(self):
        self.decisions = [
            '{"action":"write","content":"今天想留下一句安静的话。","reason":"值得记录"}',
            '{"action":"read","content":"","reason":"稍后回看"}',
        ]
        manager = self.manager(daily_write_limit=1)
        first = await manager.run_once(trigger_source="isolated_test")
        manager._state.next_eligible_at = 0
        second = await manager.run_once(trigger_source="isolated_test")
        self.assertTrue(first["executed"])
        self.assertTrue(second["executed"])
        self.assertEqual(
            [item["capability"] for item in self.service.created],
            [AUTONOMOUS_DIARY_APPEND, AUTONOMOUS_DIARY_READ],
        )
        self.assertNotIn("path", self.service.created[0]["arguments"])
        self.assertNotIn("session_key", self.service.created[0]["arguments"])
        self.assertTrue(all(item["origin"] == "autonomous:isolated_test" for item in self.service.created))
        self.assertEqual(manager._last_read[0]["content"], "今天记下了一件值得记住的小事。")

    async def test_result_path_outside_diary_is_rejected(self):
        self.decisions = ['{"action":"write","content":"x","reason":"test"}']
        manager = self.manager()

        async def unsafe_result(_task):
            return {
                "entry_id": self.service.created[0]["arguments"]["entry_id"],
                "relative_path": "../private.txt",
                "bytes_written": 1,
                "sha256": "b" * 64,
            }

        self.service.wait_for_result = unsafe_result
        result = await manager.run_once(trigger_source="isolated_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "INVALID_AGENT_RESULT")

    async def test_offline_schedules_backoff_without_calling_model(self):
        self.service._last_heartbeat = 0
        manager = self.manager()
        result = await manager.run_once(trigger_source="schedule")
        self.assertEqual(result["reason"], "body_offline_retry_scheduled")
        self.assertEqual(self.prompts, [])
        self.assertGreater(manager._state.next_eligible_at, time.time())

    async def test_daily_write_limit_blocks_second_write(self):
        self.decisions = [
            '{"action":"write","content":"first","reason":"test"}',
            '{"action":"write","content":"second","reason":"test"}',
        ]
        manager = self.manager(daily_write_limit=1)
        await manager.run_once(trigger_source="isolated_test")
        manager._state.next_eligible_at = 0
        result = await manager.run_once(trigger_source="isolated_test")
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "daily_write_limit")
        self.assertEqual(len(self.service.created), 1)

    def test_decision_parser_is_closed_and_rejects_extra_authority(self):
        for payload in (
            '{"action":"shell","content":"whoami","reason":""}',
            '{"action":"write","content":"x","reason":"","path":"C:/private"}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                AutonomousTaskManager._parse_decision(payload)


if __name__ == "__main__":
    unittest.main()
