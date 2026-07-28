from __future__ import annotations

import copy
import unittest

from demo_video_mcp.native_models import validate_native_scenario


def valid_native_scenario() -> dict:
    return {
        "schema_version": 1,
        "title": "Android tour",
        "platform": "android",
        "app_artifact_id": "artifact-1",
        "package_id": "com.example.demo",
        "device": {
            "runtime": "emulator",
            "avd": "Pixel_7_API_35",
            "orientation": "portrait",
        },
        "reset_policy": "clean",
        "steps": [
            {
                "id": "launch",
                "title": "Launch the app",
                "action": {"type": "launch"},
                "effects": ["potential_mutation"],
                "approval": "required",
                "retry_policy": "never",
            },
            {
                "id": "wait-dashboard",
                "title": "Wait for dashboard",
                "action": {
                    "type": "wait_for",
                    "target": {
                        "by": "accessibility_id",
                        "value": "Dashboard",
                    },
                },
                "effects": ["local_read"],
                "approval": "none",
                "retry_policy": "safe",
            },
        ],
    }


class NativeScenarioTests(unittest.TestCase):
    def test_valid_android_scenario(self):
        self.assertEqual(validate_native_scenario(valid_native_scenario()), [])

    def test_required_native_caption_is_valid(self):
        scenario = valid_native_scenario()
        scenario["captions"] = {
            "decision": "required",
            "reason": "운영 교육용 흐름입니다.",
            "language": "ko-KR",
            "output": "both",
        }
        scenario["steps"][1]["hold_ms"] = 1500
        scenario["steps"][1]["caption"] = {
            "screen": "대시보드",
            "text": "현재 상태를 확인합니다.",
        }
        self.assertEqual(validate_native_scenario(scenario), [])

    def test_launch_is_mandatory_and_must_be_first(self):
        scenario = valid_native_scenario()
        scenario["steps"] = scenario["steps"][1:]
        errors = validate_native_scenario(scenario)
        self.assertTrue(
            any("launch action is required" in error for error in errors)
        )

    def test_tap_requires_approval_and_no_retry(self):
        scenario = valid_native_scenario()
        scenario["steps"].append(
            {
                "id": "open-detail",
                "title": "Open detail",
                "action": {
                    "type": "tap",
                    "target": {
                        "by": "id",
                        "value": "com.example.demo:id/detail",
                    },
                },
                "effects": ["local_read"],
                "approval": "none",
                "retry_policy": "safe",
            }
        )
        errors = validate_native_scenario(scenario)
        self.assertTrue(
            any("mutation requires approval" in error for error in errors)
        )
        self.assertTrue(
            any("must never auto-retry" in error for error in errors)
        )

    def test_ios_execution_is_rejected(self):
        scenario = copy.deepcopy(valid_native_scenario())
        scenario["platform"] = "ios"
        errors = validate_native_scenario(scenario)
        self.assertTrue(
            any("only executable backend" in error for error in errors)
        )

    def test_physical_android_device_is_rejected(self):
        scenario = copy.deepcopy(valid_native_scenario())
        scenario["device"].pop("avd")
        scenario["device"]["udid"] = "R3CT10REAL"
        errors = validate_native_scenario(scenario)
        self.assertTrue(
            any("only Android Emulator" in error for error in errors)
        )


if __name__ == "__main__":
    unittest.main()
