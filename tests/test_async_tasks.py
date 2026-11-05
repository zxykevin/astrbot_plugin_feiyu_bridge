import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _install_astrbot_tool_stubs():
    modules = {
        name: sys.modules.get(name) or types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.core",
            "astrbot.core.agent",
            "astrbot.core.agent.run_context",
            "astrbot.core.agent.tool",
            "astrbot.core.astr_agent_context",
        )
    }

    class GenericBase:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    modules["astrbot.core.agent.run_context"].ContextWrapper = GenericBase
    modules["astrbot.core.agent.tool"].FunctionTool = GenericBase
    modules["astrbot.core.agent.tool"].ToolExecResult = str
    modules["astrbot.core.astr_agent_context"].AstrAgentContext = object
    sys.modules.update(modules)
    if "pydantic.dataclasses" not in sys.modules:
        pydantic_module = sys.modules.get("pydantic") or types.ModuleType("pydantic")
        pydantic_dataclasses = types.ModuleType("pydantic.dataclasses")
        pydantic_dataclasses.dataclass = dataclasses.dataclass
        pydantic_module.dataclasses = pydantic_dataclasses
        sys.modules["pydantic"] = pydantic_module
        sys.modules["pydantic.dataclasses"] = pydantic_dataclasses


_install_astrbot_tool_stubs()

from bridge.notifications import TaskNotificationManager
from bridge.service import BridgeError, BridgeService, TaskState
from bridge.tools import FeiyuTaskStatusTool, FeiyuTaskSubmitTool


class RealQqEvent:
    def __init__(self, sender_id="10001", message_id="msg-async-1"):
        self.sender_id = sender_id
        self.unified_msg_origin = f"napcat:FriendMessage:{sender_id}"
        self.message_str = "异步执行任务"
        self.message_obj = SimpleNamespace(
            message_id=message_id, raw_message={"post_type": "message"}
        )

    def get_sender_id(self):
        return self.sender_id

    def get_platform_name(self):
        return "aiocqhttp"

    def get_extra(self, key, default=None):
        return getattr(self, "extras", {}).get(key, default)


RealQqEvent.__module__ = "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"


def context_for(event):
    return SimpleNamespace(context=SimpleNamespace(event=event))


class AsyncTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(prefix="feiyu-v2-", suffix=".sqlite3")
        os.close(handle)
        self.addCleanup(self._cleanup_db)

    def _cleanup_db(self):
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.db_path + suffix)
            if path.exists():
                path.unlink()

    def service(self, *, path=None):
        return BridgeService(
            client_id="windows-dsh",
            allowed_capabilities={"dsh.workflow.run", "dsh.status.read"},
            database_path=path or self.db_path,
            recovery_grace_seconds=5,
        )

    async def submit(self, service, *, event=None, prompt="safe long task", timeout=3600):
        tool = FeiyuTaskSubmitTool(
            service=service,
            authorized_sender_ids=frozenset({"10001"}),
            default_timeout_seconds=3600,
            max_timeout_seconds=86400,
        )
        return json.loads(
            await tool.call(
                context_for(event or RealQqEvent()),
                prompt=prompt,
                timeout_seconds=timeout,
            )
        )

    async def test_submit_returns_immediately_and_long_task_remains_queryable(self):
        service = self.service()
        started = asyncio.get_running_loop().time()
        response = await self.submit(service, timeout=3600)
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], "queued")
        # Simulate a task that has existed for more than five minutes without
        # making the test suite sleep for 301 seconds.
        task = service._require_task(response["task_id"])
        task.created_at -= 301
        service.store.update(task.task_id, created_at=task.created_at)
        status = service.task_status(response["task_id"], owner="10001")
        self.assertEqual(status["status"], "queued")
        self.assertGreater(time.time() - status["created_at"], 300)
        self.assertGreater(status["deadline_at"] - status["created_at"], 300)
        service.close()

    async def test_status_query_never_reexecutes_and_duplicate_submit_is_idempotent(self):
        service = self.service()
        event = RealQqEvent(message_id="same-message")
        first = await self.submit(service, event=event)
        second = await self.submit(service, event=event)
        self.assertEqual(first["task_id"], second["task_id"])
        status_tool = FeiyuTaskStatusTool(
            service=service, authorized_sender_ids=frozenset({"10001"})
        )
        before = service.body_status()["queued_tasks"]
        queried = json.loads(
            await status_tool.call(context_for(event), task_id=first["task_id"])
        )
        self.assertTrue(queried["ok"])
        self.assertEqual(service.body_status()["queued_tasks"], before)
        service.close()

    async def test_completion_event_cannot_submit_another_dsh_task(self):
        service = self.service()
        event = RealQqEvent(message_id="completion-event")
        event.extras = {
            "feiyu_task_completion": {"task_id": "persisted-task-id"}
        }
        response = await self.submit(service, event=event)
        self.assertFalse(response["ok"])
        self.assertEqual(
            response["error"]["code"], "COMPLETION_EVENT_DSH_DENIED"
        )
        self.assertEqual(service.body_status()["queued_tasks"], 0)
        service.close()

    async def test_completion_notifies_once_and_failed_send_retries(self):
        service = self.service()
        response = await self.submit(service)
        await service.heartbeat(
            client_id="windows-dsh", capabilities=["dsh.workflow.run"]
        )
        await service.lease_task(client_id="windows-dsh", wait_seconds=0)
        await service.submit_result(
            client_id="windows-dsh",
            task_id=response["task_id"],
            status="succeeded",
            result={"value": "done", "dsh_session_id": "feiyu-bridge-session-1", "execution_id": None},
        )
        calls = []

        async def flaky(origin, message):
            calls.append((origin, message))
            return len(calls) > 1

        manager = TaskNotificationManager(
            service=service,
            send_call=flaky,
            retry_delay_seconds=1,
            max_attempts=3,
        )
        self.assertEqual(await manager.run_once(), 0)
        service.store.update(response["task_id"], notification_next_at=0)
        self.assertEqual(await manager.run_once(), 1)
        self.assertEqual(await manager.run_once(), 0)
        self.assertEqual(len(calls), 2)
        saved = service.store.get(response["task_id"])
        self.assertEqual(saved["notification_status"], "sent")
        self.assertEqual(saved["notification_attempts"], 2)
        service.close()

    async def test_cancel_request_is_distinct_from_confirmed_stop_and_unknown_fails(self):
        service = self.service()
        response = await self.submit(service)
        await service.heartbeat(
            client_id="windows-dsh", capabilities=["dsh.workflow.run"]
        )
        await service.lease_task(client_id="windows-dsh", wait_seconds=0)
        requested = await service.cancel_task(response["task_id"])
        self.assertEqual(requested.state, TaskState.CANCEL_REQUESTED)
        heartbeat = await service.heartbeat(
            client_id="windows-dsh",
            capabilities=["dsh.workflow.run"],
            active_task_id=response["task_id"],
        )
        self.assertTrue(heartbeat["cancel_requested"])
        stopped = await service.acknowledge_cancellation(
            client_id="windows-dsh", task_id=response["task_id"]
        )
        self.assertEqual(stopped.state, TaskState.CANCELLED)
        stopped.state = TaskState.UNKNOWN
        service._persist_task(stopped)
        with self.assertRaises(BridgeError) as raised:
            await service.cancel_task(stopped.task_id)
        self.assertEqual(raised.exception.code, "CANCEL_UNAVAILABLE")
        service.close()

    async def test_restart_recovers_queued_and_relinks_or_marks_running_unknown(self):
        first = self.service()
        queued = await self.submit(first, prompt="queued across restart")
        await first.heartbeat(client_id="windows-dsh", capabilities=["dsh.workflow.run"])
        leased = await first.lease_task(client_id="windows-dsh", wait_seconds=0)
        self.assertEqual(leased["task_id"], queued["task_id"])
        await first.shutdown()
        first.close()

        second = self.service()
        linked = await second.heartbeat(
            client_id="windows-dsh",
            capabilities=["dsh.workflow.run"],
            active_task_id=queued["task_id"],
        )
        self.assertEqual(linked["active_task"]["status"], "running")
        await second.shutdown()
        second.close()

        third = self.service()
        await third.heartbeat(
            client_id="windows-dsh", capabilities=["dsh.workflow.run"], active_task_id=None
        )
        self.assertEqual(third.task_status(queued["task_id"])["status"], "unknown")
        third.close()

    async def test_disconnect_queue_then_reconnect_and_unauthorized_access_denied(self):
        service = self.service()
        response = await self.submit(service)
        self.assertEqual(response["status"], "queued")
        self.assertFalse(service.body_status()["online"])
        await service.heartbeat(client_id="windows-dsh", capabilities=["dsh.workflow.run"])
        leased = await service.lease_task(client_id="windows-dsh", wait_seconds=0)
        self.assertEqual(leased["task_id"], response["task_id"])

        status_tool = FeiyuTaskStatusTool(
            service=service, authorized_sender_ids=frozenset({"10001"})
        )
        denied = json.loads(
            await status_tool.call(
                context_for(RealQqEvent(sender_id="99999")), task_id=response["task_id"]
            )
        )
        self.assertEqual(denied["error"]["code"], "SENDER_NOT_AUTHORIZED")
        with self.assertRaises(BridgeError):
            service.task_status(response["task_id"], owner="10002")
        service.close()

    async def test_restart_accepts_dsh_outcome_pending_delivery(self):
        first = self.service()
        response = await self.submit(first, prompt="finish while bridge restarts")
        await first.heartbeat(client_id="windows-dsh", capabilities=["dsh.workflow.run"])
        await first.lease_task(client_id="windows-dsh", wait_seconds=0)
        await first.shutdown()
        first.close()

        second = self.service()
        await second.heartbeat(
            client_id="windows-dsh",
            capabilities=["dsh.workflow.run"],
            active_task_id=None,
            pending_task_ids=[response["task_id"]],
        )
        self.assertEqual(second.task_status(response["task_id"])["status"], "running")
        await second.submit_result(
            client_id="windows-dsh",
            task_id=response["task_id"],
            status="succeeded",
            result={"value": "recovered delivery"},
        )
        self.assertEqual(second.task_status(response["task_id"])["status"], "completed")
        second.close()

    async def test_original_synchronous_wait_interface_still_returns_result(self):
        service = self.service()
        await service.heartbeat(client_id="windows-dsh", capabilities=["dsh.workflow.run"])
        task = await service.create_task(
            capability="dsh.workflow.run",
            arguments={"prompt": "sync", "session_key": "qq:sync", "timeout_seconds": 10},
            origin="napcat:FriendMessage:10001",
            idempotency_key="sync:compatibility",
            timeout_seconds=10,
            trusted_qq_id="10001",
        )
        await service.lease_task(client_id="windows-dsh", wait_seconds=0)
        waiter = asyncio.create_task(service.wait_for_result(task))
        await service.submit_result(
            client_id="windows-dsh",
            task_id=task.task_id,
            status="succeeded",
            result={"value": "sync-ok"},
        )
        self.assertEqual(await waiter, {"value": "sync-ok"})
        service.close()


if __name__ == "__main__":
    unittest.main()
