from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from demo_video_mcp.errors import ValidationError
from demo_video_mcp.native_runtime import (
    _inventory_from_source,
    build_android_capabilities,
    execute_native_action,
    validate_local_appium_url,
)


class FakeAppiumClient:
    def __init__(self):
        self.calls = []

    def activate_app(self, session_id, package_id):
        self.calls.append(("activate", session_id, package_id))

    def find_elements(self, session_id, using, value):
        self.calls.append(("find", session_id, using, value))
        return ["element-1"]

    def click(self, session_id, element_id):
        self.calls.append(("click", session_id, element_id))


class NativeRuntimeTests(unittest.TestCase):
    def test_remote_appium_url_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_local_appium_url("http://example.com:4723")

    def test_preserve_capabilities_do_not_auto_launch(self):
        scenario = {
            "package_id": "com.example.demo",
            "reset_policy": "preserve",
            "device": {
                "runtime": "emulator",
                "udid": "emulator-5554",
                "orientation": "portrait",
            },
        }
        capabilities = build_android_capabilities(
            scenario,
            Path("/private/tmp/demo.apk"),
            auto_launch=False,
        )
        self.assertTrue(capabilities["appium:noReset"])
        self.assertFalse(capabilities["appium:enforceAppInstall"])
        self.assertFalse(capabilities["appium:autoLaunch"])
        self.assertEqual(
            capabilities["appium:udid"],
            "emulator-5554",
        )

    def test_launch_and_tap_actions_use_appium_contract(self):
        client = FakeAppiumClient()
        with tempfile.TemporaryDirectory() as temporary:
            execute_native_action(
                client,
                "session-1",
                {"type": "launch"},
                package_id="com.example.demo",
                artifact_dir=Path(temporary),
                timeout_ms=100,
            )
            execute_native_action(
                client,
                "session-1",
                {
                    "type": "tap",
                    "target": {
                        "by": "accessibility_id",
                        "value": "Open",
                    },
                },
                package_id="com.example.demo",
                artifact_dir=Path(temporary),
                timeout_ms=100,
            )
        self.assertIn(
            ("activate", "session-1", "com.example.demo"),
            client.calls,
        )
        self.assertIn(
            ("find", "session-1", "accessibility id", "Open"),
            client.calls,
        )
        self.assertIn(("click", "session-1", "element-1"), client.calls)

    def test_inventory_redacts_editable_text_by_default(self):
        source = """
        <hierarchy>
          <node class="android.widget.Button"
                resource-id="com.example:id/start"
                content-desc="Start"
                text="Start demo"
                clickable="true"
                enabled="true" />
          <node class="android.widget.EditText"
                resource-id="com.example:id/password"
                text="secret-value"
                password="true"
                clickable="true"
                enabled="true" />
        </hierarchy>
        """
        inventory = _inventory_from_source(
            source,
            include_text=True,
            maximum_items=10,
        )
        password = inventory["controls"][1]
        self.assertIsNone(password["text"])
        self.assertNotIn("secret-value", str(inventory))


if __name__ == "__main__":
    unittest.main()
