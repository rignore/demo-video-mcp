from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demo_video_mcp.server import build_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = {
            "DEMO_VIDEO_PROJECT_ROOT": str(PROJECT_ROOT),
            "DEMO_VIDEO_DATA_ROOT": str(
                Path(self.temporary.name) / "data"
            ),
            "DEMO_VIDEO_PLUGIN_DIRS": str(PROJECT_ROOT / "plugins"),
            "DEMO_VIDEO_ALLOWED_ROOTS": str(PROJECT_ROOT),
        }
        self.patch = patch.dict(os.environ, environment, clear=False)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.server = build_server()

    def test_initialize_and_tool_discovery(self):
        initialized = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        self.assertEqual(
            initialized["result"]["protocolVersion"],
            "2025-11-25",
        )
        listed = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("start_video_login", names)
        self.assertIn("start_video_job", names)
        self.assertIn("get_video_planning_context", names)
        self.assertIn("inspect_video_site", names)
        self.assertIn("register_native_app", names)
        self.assertIn("get_native_runtime_status", names)
        self.assertIn("inspect_native_app", names)
        self.assertIn("create_native_video_job", names)
        native_inspection_tool = next(
            tool
            for tool in listed["result"]["tools"]
            if tool["name"] == "inspect_native_app"
        )
        self.assertTrue(
            native_inspection_tool["annotations"]["destructiveHint"]
        )
        self.assertIn(
            "confirm_app_launch",
            native_inspection_tool["inputSchema"]["required"],
        )
        planning_tool = next(
            tool
            for tool in listed["result"]["tools"]
            if tool["name"] == "get_video_planning_context"
        )
        self.assertIn(
            "capture",
            planning_tool["inputSchema"]["properties"],
        )
        inspection_tool = next(
            tool
            for tool in listed["result"]["tools"]
            if tool["name"] == "inspect_video_site"
        )
        self.assertTrue(
            inspection_tool["annotations"]["readOnlyHint"]
        )
        self.assertFalse(
            inspection_tool["annotations"]["idempotentHint"]
        )

    def test_structured_tool_result(self):
        response = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_video_plugins",
                    "arguments": {},
                },
            }
        )
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertIn("plugins", result["structuredContent"])

    def test_brief_resource_and_prompt_are_discoverable(self):
        resources = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/list",
                "params": {},
            }
        )
        uris = {
            item["uri"]
            for item in resources["result"]["resources"]
        }
        self.assertIn("demo-video://schemas/brief/v1", uris)
        self.assertIn("demo-video://schemas/native-scenario/v1", uris)
        self.assertIn("demo-video://schemas/app-artifact/v1", uris)
        prompts = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "prompts/list",
                "params": {},
            }
        )
        names = {
            item["name"] for item in prompts["result"]["prompts"]
        }
        self.assertIn("record-video-from-brief", names)
        self.assertIn("record-native-video-from-brief", names)


if __name__ == "__main__":
    unittest.main()
