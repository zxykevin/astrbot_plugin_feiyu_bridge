import json
import dataclasses
import sys
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

from bridge.service import BridgeService
from bridge.tools import DshExecuteTool


class RecordingService(BridgeService):
    def __init__(self):
        super().__init__(
            client_id="windows-dsh",
            allowed_capabilities={"dsh.status.read", "dsh.workflow.run"},
        )
        self.created = []

    async def create_task(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(task_id=f"task-{len(self.created)}")

    async def wait_for_result(self, task):
        return {"value": "ok", "task": task.task_id}


class RealQqEvent:
    def __init__(
        self,
        *,
        sender_id="10001",
        origin="napcat:FriendMessage:10001",
        message="请调用电脑",
        message_id="msg-1",
    ):
        self.sender_id = sender_id
        self.unified_msg_origin = origin
        self.message_str = message
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            raw_message={"post_type": "message"},
        )

    def get_sender_id(self):
        return self.sender_id

    def get_platform_name(self):
        return "aiocqhttp"


RealQqEvent.__module__ = (
    "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
)


class CronMessageEvent(RealQqEvent):
    def get_platform_name(self):
        return "cron"


CronMessageEvent.__module__ = "astrbot.core.cron.events"


class ProactiveMessageEvent(RealQqEvent):
    pass


ProactiveMessageEvent.__module__ = "data.plugins.astrbot_plugin_proactive_chat"


def context_for(event):
    return SimpleNamespace(context=SimpleNamespace(event=event))


class DshExecuteToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = RecordingService()
        self.tool = DshExecuteTool(
            service=self.service,
            authorized_sender_ids=frozenset({"10001", "10002"}),
            default_timeout_seconds=30,
            max_timeout_seconds=120,
        )

    def test_model_schema_is_explicit_and_does_not_expose_session(self):
        self.assertEqual(set(self.tool.parameters["properties"]), {"prompt", "timeout_seconds"})
        self.assertEqual(self.tool.parameters["required"], ["prompt"])
        self.assertFalse(self.tool.parameters["additionalProperties"])

    def test_fixed_session_derivation_vector(self):
        vector_path = Path(__file__).parent / "vectors" / "workflow_v1.json"
        vector = json.loads(vector_path.read_text(encoding="utf-8"))
        identity = vector["identity"]
        actual = self.tool._derive_session_key(
            identity["sender_id"], identity["origin"]
        )
        self.assertEqual(actual, vector["expected_session_key"])

    async def test_server_injects_stable_session_key(self):
        event = RealQqEvent()
        await self.tool.call(context_for(event), prompt="first", timeout_seconds=40)
        await self.tool.call(context_for(event), prompt="second", timeout_seconds=40)
        first, second = self.service.created
        self.assertEqual(first["capability"], "dsh.workflow.run")
        self.assertEqual(first["arguments"]["session_key"], second["arguments"]["session_key"])
        self.assertRegex(first["arguments"]["session_key"], r"^qq:[0-9a-f]{60}$")
        self.assertEqual(first["arguments"]["timeout_seconds"], 40)

    async def test_users_and_conversations_have_distinct_sessions(self):
        events = (
            RealQqEvent(sender_id="10001", origin="napcat:GroupMessage:20001"),
            RealQqEvent(sender_id="10002", origin="napcat:GroupMessage:20001"),
            RealQqEvent(sender_id="10001", origin="napcat:GroupMessage:20002"),
        )
        for index, event in enumerate(events):
            await self.tool.call(
                context_for(event), prompt=f"request {index}", timeout_seconds=30
            )
        keys = {
            item["arguments"]["session_key"] for item in self.service.created
        }
        self.assertEqual(len(keys), 3)

    async def test_unauthorized_user_is_rejected_before_queueing(self):
        response = json.loads(
            await self.tool.call(
                context_for(RealQqEvent(sender_id="99999")), prompt="request"
            )
        )
        self.assertEqual(response["error"]["code"], "SENDER_NOT_AUTHORIZED")
        self.assertEqual(self.service.created, [])

    async def test_cron_and_proactive_events_cannot_bypass_authorization(self):
        for event in (CronMessageEvent(), ProactiveMessageEvent()):
            with self.subTest(event=type(event).__name__):
                response = json.loads(
                    await self.tool.call(context_for(event), prompt="request")
                )
                self.assertEqual(response["error"]["code"], "DIRECT_USER_REQUIRED")
        self.assertEqual(self.service.created, [])

    async def test_empty_synthetic_context_is_rejected(self):
        response = json.loads(await self.tool.call(context_for(None), prompt="request"))
        self.assertEqual(response["error"]["code"], "DIRECT_USER_REQUIRED")
        self.assertEqual(self.service.created, [])

    async def test_runtime_rejects_invalid_prompt_and_timeout(self):
        event = RealQqEvent()
        for kwargs in (
            {"prompt": ""},
            {"prompt": "hello", "timeout_seconds": 1.5},
            {"prompt": "hello\x00"},
        ):
            with self.subTest(kwargs=kwargs):
                response = json.loads(await self.tool.call(context_for(event), **kwargs))
                self.assertEqual(response["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.service.created, [])


if __name__ == "__main__":
    unittest.main()
