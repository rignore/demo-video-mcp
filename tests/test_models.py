from __future__ import annotations

import unittest

from demo_video_mcp.briefs import (
    normalize_video_brief,
    validate_video_brief,
)
from demo_video_mcp.models import canonical_hash, validate_scenario


def base_scenario(step):
    return {
        "schema_version": 1,
        "title": "Test",
        "start_url": "https://example.com",
        "allowed_origins": ["https://example.com"],
        "steps": [step],
    }


class ScenarioValidationTests(unittest.TestCase):
    def test_read_only_step_is_valid(self):
        scenario = base_scenario(
            {
                "id": "wait-heading",
                "title": "Wait",
                "action": {
                    "type": "wait_for",
                    "target": {
                        "by": "role",
                        "value": "heading",
                        "name": "Demo",
                    },
                },
                "effects": ["remote_read"],
                "approval": "none",
                "retry_policy": "safe",
            }
        )
        self.assertEqual(
            validate_scenario(
                scenario,
                plugin_allowed_origins=["*"],
            ),
            [],
        )

    def test_required_captions_need_reviewable_scene_text(self):
        scenario = base_scenario(
            {
                "id": "wait-heading",
                "title": "Wait",
                "action": {
                    "type": "wait_for",
                    "target": {
                        "by": "role",
                        "value": "heading",
                        "name": "Demo",
                    },
                },
                "effects": ["remote_read"],
                "approval": "none",
                "retry_policy": "safe",
                "hold_ms": 1500,
                "caption": {
                    "screen": "Demo dashboard",
                    "text": "Review the current status.",
                },
            }
        )
        scenario["captions"] = {
            "decision": "required",
            "reason": "The external audience needs guided context.",
            "output": "both",
        }
        self.assertEqual(
            validate_scenario(
                scenario,
                plugin_allowed_origins=["*"],
            ),
            [],
        )

        scenario["steps"][0]["hold_ms"] = 800
        errors = validate_scenario(
            scenario,
            plugin_allowed_origins=["*"],
        )
        self.assertTrue(
            any("at least 1200ms" in error for error in errors)
        )

    def test_interaction_cannot_lower_risk(self):
        scenario = base_scenario(
            {
                "id": "click-button",
                "title": "Click",
                "action": {
                    "type": "click",
                    "target": {
                        "by": "role",
                        "value": "button",
                        "name": "Next",
                    },
                },
                "effects": ["remote_read"],
                "approval": "none",
                "retry_policy": "safe",
            }
        )
        errors = validate_scenario(
            scenario,
            plugin_allowed_origins=["*"],
        )
        self.assertTrue(
            any("mutation requires approval" in error for error in errors)
        )
        self.assertTrue(
            any("must never auto-retry" in error for error in errors)
        )

    def test_disallows_navigation_to_another_origin(self):
        scenario = base_scenario(
            {
                "id": "leave-origin",
                "title": "Leave",
                "action": {
                    "type": "goto",
                    "url": "https://other.example/path",
                },
                "effects": ["potential_mutation"],
                "approval": "required",
                "retry_policy": "never",
            }
        )
        errors = validate_scenario(
            scenario,
            plugin_allowed_origins=["*"],
        )
        self.assertTrue(
            any("origin is not allowed" in error for error in errors)
        )

    def test_scenario_url_rejects_embedded_credentials(self):
        scenario = base_scenario(
            {
                "id": "wait-heading",
                "title": "Wait",
                "action": {
                    "type": "wait_for",
                    "target": {
                        "by": "role",
                        "value": "heading",
                        "name": "Demo",
                    },
                },
                "effects": ["remote_read"],
                "approval": "none",
                "retry_policy": "safe",
            }
        )
        scenario["start_url"] = "https://user:password@example.com"
        errors = validate_scenario(
            scenario,
            plugin_allowed_origins=["*"],
        )
        self.assertTrue(
            any("absolute http/https URL" in error for error in errors)
        )

    def test_plan_hash_is_deterministic(self):
        left = canonical_hash({"b": 2, "a": 1})
        right = canonical_hash({"a": 1, "b": 2})
        self.assertEqual(left, right)

    def test_mobile_capture_is_valid(self):
        scenario = base_scenario(
            {
                "id": "wait-heading",
                "title": "Wait",
                "action": {
                    "type": "wait_for",
                    "target": {
                        "by": "role",
                        "value": "heading",
                        "name": "Demo",
                    },
                },
                "effects": ["remote_read"],
                "approval": "none",
                "retry_policy": "safe",
            }
        )
        scenario["capture"] = {
            "target": "mobile",
            "device": "pixel-7",
            "orientation": "portrait",
        }
        self.assertEqual(
            validate_scenario(
                scenario,
                plugin_allowed_origins=["*"],
            ),
            [],
        )

    def test_capture_device_must_match_target(self):
        scenario = base_scenario(
            {
                "id": "wait-heading",
                "title": "Wait",
                "action": {
                    "type": "wait_for",
                    "target": {
                        "by": "role",
                        "value": "heading",
                        "name": "Demo",
                    },
                },
                "effects": ["remote_read"],
                "approval": "none",
                "retry_policy": "safe",
            }
        )
        scenario["capture"] = {
            "target": "mobile",
            "device": "desktop-chrome",
        }
        errors = validate_scenario(
            scenario,
            plugin_allowed_origins=["*"],
        )
        self.assertTrue(any("not mobile" in error for error in errors))

    def test_non_standard_base_viewport_is_rejected(self):
        scenario = base_scenario(
            {
                "id": "wait-heading",
                "title": "Wait",
                "action": {
                    "type": "wait_for",
                    "target": {
                        "by": "role",
                        "value": "heading",
                        "name": "Demo",
                    },
                },
                "effects": ["remote_read"],
                "approval": "none",
                "retry_policy": "safe",
            }
        )
        scenario["viewport"] = {"width": 1280, "height": 720}
        errors = validate_scenario(
            scenario,
            plugin_allowed_origins=["*"],
        )
        self.assertTrue(
            any("exactly 1920x1080" in error for error in errors)
        )

    def test_video_brief_defaults_are_normalized(self):
        brief = {
            "schema_version": 1,
            "purpose": " Explain the workflow ",
            "audience": " New operators ",
            "key_messages": [" Fast response "],
        }
        self.assertEqual(validate_video_brief(brief), [])
        normalized = normalize_video_brief(brief)
        self.assertEqual(normalized["duration_seconds"], 60)
        self.assertEqual(normalized["purpose"], "Explain the workflow")
        self.assertEqual(normalized["key_messages"], ["Fast response"])

    def test_video_brief_rejects_unknown_fields(self):
        brief = {
            "schema_version": 1,
            "purpose": "Explain",
            "audience": "Operators",
            "key_messages": ["Fast response"],
            "selector": "#unsafe",
        }
        errors = validate_video_brief(brief)
        self.assertTrue(
            any("unsupported properties" in error for error in errors)
        )

    def test_video_brief_rejects_duplicates_after_trimming(self):
        brief = {
            "schema_version": 1,
            "purpose": "Explain",
            "audience": "Operators",
            "key_messages": ["Fast response", " Fast response "],
        }
        errors = validate_video_brief(brief)
        self.assertTrue(
            any("duplicate items" in error for error in errors)
        )


if __name__ == "__main__":
    unittest.main()
