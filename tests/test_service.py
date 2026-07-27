from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from demo_video_mcp.config import Settings
from demo_video_mcp.errors import ConflictError, ValidationError
from demo_video_mcp.service import VideoService
from demo_video_mcp.storage import atomic_write_json, read_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = VideoService(
            Settings(
                project_root=PROJECT_ROOT,
                data_root=Path(self.temporary.name) / "data",
                plugin_dirs=(PROJECT_ROOT / "plugins",),
                allowed_guide_roots=(PROJECT_ROOT,),
                allow_file_urls=False,
            )
        )

    def test_read_only_template_becomes_ready(self):
        job = self.service.create_job(
            plugin_id="generic-web",
            template_id="read-only-tour",
            variables={
                "start_url": "https://example.com",
                "allowed_origin": "https://example.com",
                "heading": "Example",
            },
        )
        result = self.service.preflight_job(job["job_id"])
        self.assertEqual(result["job"]["state"], "READY")
        self.assertEqual(result["preflight"]["mutations"], [])

    def test_mutation_requires_exact_approval(self):
        scenario = {
            "schema_version": 1,
            "title": "Mutation",
            "start_url": "https://example.com",
            "allowed_origins": ["https://example.com"],
            "steps": [
                {
                    "id": "submit",
                    "title": "Submit",
                    "action": {
                        "type": "click",
                        "target": {
                            "by": "role",
                            "value": "button",
                            "name": "Submit",
                        },
                    },
                    "effects": ["remote_write"],
                    "approval": "required",
                    "retry_policy": "never",
                }
            ],
        }
        job = self.service.create_job(
            plugin_id="generic-web",
            scenario=scenario,
        )
        result = self.service.preflight_job(job["job_id"])
        self.assertEqual(result["job"]["state"], "AWAITING_APPROVAL")
        with self.assertRaises(ConflictError):
            self.service.approve_job(
                job["job_id"],
                "wrong-plan",
                ["submit"],
                True,
            )
        approved = self.service.approve_job(
            job["job_id"],
            job["plan_hash"],
            ["submit"],
            True,
        )
        self.assertEqual(approved["state"], "READY")

    def test_optional_protectgo_template_is_a_valid_plugin_job(self):
        job = self.service.create_job(
            plugin_id="protectgo",
            template_id="situation-response",
            variables={
                "origin": "https://protectgo.kr",
                "project_name": "dev",
                "target_title": "Disposable fixture",
                "target_situation_id": "fixture-1",
                "summary": "Fixture summary",
                "description": "Fixture description",
            },
        )
        result = self.service.preflight_job(job["job_id"])
        self.assertEqual(result["job"]["state"], "AWAITING_APPROVAL")
        self.assertGreater(len(result["preflight"]["mutations"]), 0)

    def test_planning_context_accepts_a_short_brief_and_mobile_target(self):
        result = self.service.get_planning_context(
            plugin_id="generic-web",
            brief={
                "schema_version": 1,
                "purpose": "Explain the dashboard",
                "audience": "First-time operators",
                "key_messages": ["The current state is easy to find"],
            },
            start_url="https://example.com",
            capture={"target": "mobile"},
        )
        self.assertEqual(result["capture"]["target"], "mobile")
        self.assertEqual(result["capture"]["device"], "pixel-7")
        self.assertEqual(
            result["scenario_seed"]["capture"],
            result["capture"],
        )
        self.assertEqual(
            result["delivery"]["output_size"],
            {"width": 1920, "height": 1080},
        )
        self.assertEqual(result["missing_inputs"], [])
        self.assertTrue(result["guides"])

    def test_template_capture_override_is_frozen_in_scenario(self):
        job = self.service.create_job(
            plugin_id="generic-web",
            template_id="read-only-tour",
            variables={
                "start_url": "https://example.com",
                "allowed_origin": "https://example.com",
                "heading": "Example",
            },
            capture={
                "target": "mobile",
                "device": "pixel-7",
                "orientation": "portrait",
            },
        )
        self.assertEqual(job["summary"]["capture"]["target"], "mobile")
        scenario = self.service.store.get_scenario(job["job_id"])
        self.assertEqual(scenario["capture"]["device"], "pixel-7")

    def test_brief_is_bound_to_plan_hash(self):
        common = {
            "plugin_id": "generic-web",
            "template_id": "read-only-tour",
            "variables": {
                "start_url": "https://example.com",
                "allowed_origin": "https://example.com",
                "heading": "Example",
            },
        }
        first = self.service.create_job(
            **common,
            brief={
                "schema_version": 1,
                "purpose": "Explain onboarding",
                "audience": "New operators",
                "key_messages": ["Find the dashboard"],
            },
        )
        second = self.service.create_job(
            **common,
            brief={
                "schema_version": 1,
                "purpose": "Explain incident review",
                "audience": "New operators",
                "key_messages": ["Find the dashboard"],
            },
        )
        self.assertNotEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(first["brief"]["purpose"], "Explain onboarding")

    def test_planning_url_rejects_embedded_credentials(self):
        with self.assertRaises(ValidationError):
            self.service.get_planning_context(
                plugin_id="generic-web",
                brief={
                    "schema_version": 1,
                    "purpose": "Explain",
                    "audience": "Operators",
                    "key_messages": ["Safe access"],
                },
                start_url="https://user:password@example.com",
            )

    def test_scenario_profile_id_is_used_by_recording_request(self):
        scenario = self.service.registry.get("generic-web").load_template(
            "read-only-tour",
            {
                "start_url": "https://example.com",
                "allowed_origin": "https://example.com",
                "heading": "Example",
            },
        )
        scenario["profile_id"] = "scenario-profile"
        job = self.service.create_job(
            plugin_id="generic-web",
            scenario=scenario,
        )
        request = read_json(
            self.service.store.job_dir(job["job_id"]) / "request.json"
        )
        self.assertEqual(request["profile_id"], "scenario-profile")

    def test_scenario_and_tool_profile_ids_must_match(self):
        scenario = self.service.registry.get("generic-web").load_template(
            "read-only-tour",
            {
                "start_url": "https://example.com",
                "allowed_origin": "https://example.com",
                "heading": "Example",
            },
        )
        scenario["profile_id"] = "scenario-profile"
        with self.assertRaises(ValidationError):
            self.service.create_job(
                plugin_id="generic-web",
                scenario=scenario,
                profile_id="tool-profile",
            )

    def test_auth_status_redacts_oauth_query_and_uses_private_mode(self):
        status = self.service.store.create_auth_session(
            "oauth",
            "https://example.com/login?code=secret#fragment",
        )
        session_dir = self.service.store.auth_session_dir(
            status["session_id"]
        )
        self.assertEqual(
            status["login_url"],
            "https://example.com/login",
        )
        request = read_json(session_dir / "request.json")
        self.assertIn("code=secret", request["login_url"])
        updated = self.service.store.update_auth_status(
            status["session_id"],
            current_url=(
                "https://example.com/callback?code=oauth-secret#token"
            ),
            error=(
                "Page.goto failed at "
                "https://example.com/login?token=error-secret"
            ),
        )
        self.assertEqual(
            updated["current_url"],
            "https://example.com/callback",
        )
        self.assertNotIn("error-secret", updated["error"])
        self.assertNotIn(
            "error-secret",
            (session_dir / "status.json").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (session_dir / "status.json").stat().st_mode & 0o077,
            0,
        )
        self.assertEqual(
            (session_dir / "request.json").stat().st_mode & 0o077,
            0,
        )

    def test_runtime_auth_state_cannot_be_read_as_a_guide(self):
        state_path = (
            self.service.settings.data_root
            / "profiles"
            / "dev"
            / "state.json"
        )
        atomic_write_json(
            state_path,
            {"cookies": [{"value": "secret"}]},
            mode=0o600,
        )
        with self.assertRaises(ValidationError):
            self.service.read_guide(str(state_path))

    def test_profile_metadata_is_sanitized_on_read(self):
        profile_dir = self.service.store.profile_dir("legacy")
        atomic_write_json(
            profile_dir / "profile.json",
            {
                "profile_id": "legacy",
                "origin": "https://example.com",
                "login_url": "https://example.com/login?token=secret",
                "current_url": (
                    "https://example.com/callback?code=oauth-secret#token"
                ),
            },
        )
        atomic_write_json(
            profile_dir / "state.json",
            {"cookies": [], "origins": []},
            mode=0o600,
        )
        profile = self.service.store.get_profile("legacy")
        self.assertEqual(
            profile["login_url"],
            "https://example.com/login",
        )
        self.assertEqual(
            profile["current_url"],
            "https://example.com/callback",
        )
        self.assertEqual(
            (profile_dir / "profile.json").stat().st_mode & 0o077,
            0,
        )


if __name__ == "__main__":
    unittest.main()
