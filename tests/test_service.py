import asyncio
import unittest

from bridge.protocol import AUTONOMOUS_DIARY_APPEND
from bridge.service import BridgeError, BridgeService, TaskState


class BridgeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = BridgeService(
            client_id="windows-dsh",
            allowed_capabilities={"dsh.status.read", "dsh.workflow.run"},
            heartbeat_timeout_seconds=25,
            max_queue_size=2,
        )
        await self.service.heartbeat(
            client_id="windows-dsh",
            capabilities=["dsh.workflow.run", "shell.exec"],
            agent_version="test",
        )

    async def test_online_status_intersects_allowlists(self):
        status = self.service.body_status()
        self.assertTrue(status["online"])
        self.assertEqual(status["capabilities"], ["dsh.workflow.run"])

    async def test_task_round_trip(self):
        task = await self.service.create_task(
            capability="dsh.workflow.run",
            arguments={
                "prompt": "read-only status",
                "session_key": "qq:test-user-session",
                "timeout_seconds": 5,
            },
            origin="test:FriendMessage:1",
            idempotency_key="request:12345678",
            timeout_seconds=5,
        )
        leased = await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        self.assertEqual(leased["task_id"], task.task_id)
        await self.service.submit_result(
            client_id="windows-dsh",
            task_id=task.task_id,
            status="succeeded",
            result={"answer": 42},
        )
        self.assertEqual(await self.service.wait_for_result(task), {"answer": 42})
        self.assertEqual(task.state, TaskState.SUCCEEDED)

    async def test_idempotent_create_returns_same_task(self):
        kwargs = dict(
            capability="dsh.workflow.run",
            arguments={"prompt": "x", "session_key": "qq:test-session"},
            origin="test:FriendMessage:1",
            idempotency_key="request:abcdefgh",
            timeout_seconds=5,
        )
        first = await self.service.create_task(**kwargs)
        second = await self.service.create_task(**kwargs)
        self.assertIs(first, second)

    async def test_idempotency_conflict_is_rejected(self):
        common = dict(
            capability="dsh.workflow.run",
            origin="test:FriendMessage:1",
            idempotency_key="request:conflict",
            timeout_seconds=5,
        )
        await self.service.create_task(
            arguments={"prompt": "one", "session_key": "qq:test-session"},
            **common,
        )
        with self.assertRaises(BridgeError) as raised:
            await self.service.create_task(
                arguments={"prompt": "two", "session_key": "qq:test-session"},
                **common,
            )
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    async def test_single_concurrency(self):
        first = await self.service.create_task(
            capability="dsh.workflow.run",
            arguments={"prompt": "one", "session_key": "qq:first-session"},
            origin="test:FriendMessage:1",
            idempotency_key="request:11111111",
            timeout_seconds=5,
        )
        await self.service.create_task(
            capability="dsh.workflow.run",
            arguments={"prompt": "two", "session_key": "qq:second-session"},
            origin="test:FriendMessage:1",
            idempotency_key="request:22222222",
            timeout_seconds=5,
        )
        leased = await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        self.assertEqual(leased["task_id"], first.task_id)
        self.assertIsNone(
            await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        )

    async def test_cancel_ack_releases_single_lease(self):
        first = await self.service.create_task(
            capability="dsh.workflow.run",
            arguments={"prompt": "one", "session_key": "qq:first-session"},
            origin="test:FriendMessage:1",
            idempotency_key="request:cancel01",
            timeout_seconds=5,
        )
        second = await self.service.create_task(
            capability="dsh.workflow.run",
            arguments={"prompt": "two", "session_key": "qq:second-session"},
            origin="test:FriendMessage:1",
            idempotency_key="request:cancel02",
            timeout_seconds=5,
        )
        await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        await self.service.cancel_task(first.task_id)
        self.assertIsNone(
            await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        )
        await self.service.acknowledge_cancellation(
            client_id="windows-dsh", task_id=first.task_id
        )
        leased = await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        self.assertEqual(leased["task_id"], second.task_id)

    async def test_denied_capability(self):
        with self.assertRaises(ValueError):
            BridgeService(
                client_id="windows-dsh",
                allowed_capabilities={"shell.exec"},
            )

    async def test_safe_but_unsupported_capability_is_denied(self):
        with self.assertRaises(ValueError):
            BridgeService(
                client_id="windows-dsh",
                allowed_capabilities={"dsh.browser.navigate"},
            )

    async def test_workflow_shape_requires_trusted_session_key(self):
        with self.assertRaises(ValueError):
            await self.service.create_task(
                capability="dsh.workflow.run",
                arguments={"prompt": "hello"},
                origin="test:FriendMessage:1",
                idempotency_key="request:session1",
                timeout_seconds=5,
            )

    async def test_diary_result_path_is_validated_before_success(self):
        service = BridgeService(
            client_id="windows-dsh",
            allowed_capabilities={AUTONOMOUS_DIARY_APPEND},
        )
        await service.heartbeat(
            client_id="windows-dsh",
            capabilities=[AUTONOMOUS_DIARY_APPEND],
            agent_version="test",
        )
        entry_id = "12345678-1234-1234-1234-123456789abc"
        task = await service.create_task(
            capability=AUTONOMOUS_DIARY_APPEND,
            arguments={
                "entry_id": entry_id,
                "content": "test",
                "created_at": "2026-11-05T12:30:00+01:00",
            },
            origin="autonomous:isolated_test",
            idempotency_key="autonomous:result-check",
            timeout_seconds=5,
        )
        await service.lease_task(client_id="windows-dsh", wait_seconds=0)
        with self.assertRaises(ValueError):
            await service.submit_result(
                client_id="windows-dsh",
                task_id=task.task_id,
                status="succeeded",
                result={
                    "entry_id": entry_id,
                    "relative_path": "../private.txt",
                    "bytes_written": 4,
                    "sha256": "a" * 64,
                },
            )
        self.assertEqual(task.state, TaskState.LEASED)

    async def test_offline_degradation(self):
        self.service._last_heartbeat = 0
        with self.assertRaises(BridgeError) as raised:
            await self.service.create_task(
                capability="dsh.workflow.run",
                arguments={"prompt": "offline", "session_key": "qq:offline"},
                origin="test:FriendMessage:1",
                idempotency_key="request:offline1",
                timeout_seconds=5,
            )
        self.assertEqual(raised.exception.code, "BODY_OFFLINE")

    async def _leased_task(self, key="request:race0001", timeout=5):
        task = await self.service.create_task(
            capability="dsh.workflow.run",
            arguments={
                "prompt": "race test",
                "session_key": "qq:race-session",
                "timeout_seconds": timeout,
            },
            origin="test:FriendMessage:1",
            idempotency_key=key,
            timeout_seconds=timeout,
        )
        await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        return task

    async def test_identical_result_retry_is_idempotent(self):
        task = await self._leased_task()
        kwargs = dict(
            client_id="windows-dsh",
            task_id=task.task_id,
            status="succeeded",
            result={"value": "same", "session_key": "qq:race-session"},
        )
        first = await self.service.submit_result(**kwargs)
        second = await self.service.submit_result(**kwargs)
        self.assertIs(first, second)
        self.assertEqual(second.state, TaskState.SUCCEEDED)

    async def test_conflicting_result_retry_is_rejected(self):
        task = await self._leased_task()
        await self.service.submit_result(
            client_id="windows-dsh",
            task_id=task.task_id,
            status="succeeded",
            result={"value": "first"},
        )
        with self.assertRaises(BridgeError) as raised:
            await self.service.submit_result(
                client_id="windows-dsh",
                task_id=task.task_id,
                status="succeeded",
                result={"value": "different"},
            )
        self.assertEqual(raised.exception.code, "TASK_ALREADY_TERMINAL")

    async def test_cancel_wins_over_late_result(self):
        task = await self._leased_task()
        await self.service.cancel_task(task.task_id)
        with self.assertRaises(BridgeError) as raised:
            await self.service.submit_result(
                client_id="windows-dsh",
                task_id=task.task_id,
                status="succeeded",
                result={"value": "late"},
            )
        self.assertEqual(raised.exception.code, "TASK_ALREADY_TERMINAL")
        acknowledged = await self.service.acknowledge_cancellation(
            client_id="windows-dsh", task_id=task.task_id
        )
        self.assertEqual(acknowledged.state, TaskState.CANCELLED)

    async def test_result_wins_over_late_cancel(self):
        task = await self._leased_task()
        await self.service.submit_result(
            client_id="windows-dsh",
            task_id=task.task_id,
            status="succeeded",
            result={"value": "on time"},
        )
        terminal = await self.service.cancel_task(task.task_id)
        self.assertEqual(terminal.state, TaskState.SUCCEEDED)
        with self.assertRaises(BridgeError) as raised:
            await self.service.acknowledge_cancellation(
                client_id="windows-dsh", task_id=task.task_id
            )
        self.assertEqual(raised.exception.code, "TASK_NOT_CANCELLED")

    async def test_timeout_keeps_single_slot_until_ack(self):
        first = await self._leased_task(timeout=1)
        second = await self.service.create_task(
            capability="dsh.workflow.run",
            arguments={"prompt": "next", "session_key": "qq:next-session"},
            origin="test:FriendMessage:2",
            idempotency_key="request:race0002",
            timeout_seconds=5,
        )
        first.deadline_at = 0
        heartbeat = await self.service.heartbeat(
            client_id="windows-dsh",
            capabilities=["dsh.status.read", "dsh.workflow.run"],
            active_task_id=first.task_id,
        )
        self.assertTrue(heartbeat["cancel_requested"])
        self.assertEqual(first.state, TaskState.TIMED_OUT)
        self.assertIsNone(
            await self.service.lease_task(client_id="windows-dsh", wait_seconds=0)
        )
        await self.service.acknowledge_cancellation(
            client_id="windows-dsh", task_id=first.task_id
        )
        leased = await self.service.lease_task(
            client_id="windows-dsh", wait_seconds=0
        )
        self.assertEqual(leased["task_id"], second.task_id)

    async def test_deadline_wins_over_late_result(self):
        task = await self._leased_task(timeout=1)
        task.deadline_at = 0
        with self.assertRaises(BridgeError) as raised:
            await self.service.submit_result(
                client_id="windows-dsh",
                task_id=task.task_id,
                status="succeeded",
                result={"value": "too late"},
            )
        self.assertEqual(raised.exception.code, "TASK_ALREADY_TERMINAL")
        self.assertEqual(task.state, TaskState.TIMED_OUT)


if __name__ == "__main__":
    unittest.main()

