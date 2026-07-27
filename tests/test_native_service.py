from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from demo_video_mcp.config import Settings
from demo_video_mcp.errors import ValidationError
from demo_video_mcp.service import VideoService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ready_runtime() -> dict:
    names = [
        "adb",
        "appium",
        "uiautomator2",
        "android_emulator",
        "ffmpeg",
    ]
    checks = [
        {"name": name, "status": "passed", "details": []}
        for name in names
    ]
    return {
        "backend": "native-android",
        "ready": True,
        "appium_server_url": "http://127.0.0.1:4723",
        "appium_server_reachable": True,
        "appium_server_error": None,
        "connected_devices": ["emulator-5554"],
        "available_avds": [],
        "checks": checks,
    }


def native_scenario(artifact_id: str) -> dict:
    return {
        "schema_version": 1,
        "title": "Native fixture",
        "platform": "android",
        "app_artifact_id": artifact_id,
        "package_id": "com.example.demo",
        "device": {
            "runtime": "emulator",
            "udid": "emulator-5554",
            "orientation": "portrait",
        },
        "steps": [
            {
                "id": "launch",
                "title": "Launch app",
                "action": {"type": "launch"},
                "effects": ["potential_mutation"],
                "approval": "required",
                "retry_policy": "never",
            }
        ],
    }


class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = VideoService(
            Settings(
                project_root=PROJECT_ROOT,
                data_root=self.root / "data",
                plugin_dirs=(PROJECT_ROOT / "plugins",),
                allowed_guide_roots=(PROJECT_ROOT,),
                allow_file_urls=False,
            )
        )
        self.apk = self.root / "demo.apk"
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"fixture")
            archive.writestr("classes.dex", b"fixture")

    def register(self) -> dict:
        return self.service.register_native_app(
            platform="android",
            path=str(self.apk),
        )["artifact"]

    def test_apk_registration_copies_to_private_storage(self):
        artifact = self.register()
        self.assertNotIn("path", artifact)
        stored = self.service.store.get_native_app(
            artifact["artifact_id"]
        )
        copied = Path(stored["path"])
        self.assertTrue(copied.is_file())
        self.assertTrue(
            copied.is_relative_to(self.service.store.native_apps_root)
        )
        self.assertEqual(copied.stat().st_mode & 0o077, 0)
        self.assertNotIn(str(self.apk), str(artifact))
        duplicate = self.register()
        self.assertEqual(duplicate["artifact_id"], artifact["artifact_id"])

    def test_native_job_reuses_approval_state_machine(self):
        artifact = self.register()
        job = self.service.create_native_job(
            scenario=native_scenario(artifact["artifact_id"])
        )
        with patch(
            "demo_video_mcp.service.native_runtime_status",
            return_value=ready_runtime(),
        ):
            result = self.service.preflight_job(job["job_id"])
        self.assertEqual(
            result["job"]["state"],
            "AWAITING_APPROVAL",
        )
        self.assertEqual(
            [item["step_id"] for item in result["preflight"]["mutations"]],
            ["launch"],
        )
        approved = self.service.approve_job(
            job["job_id"],
            job["plan_hash"],
            ["launch"],
            True,
        )
        self.assertEqual(approved["state"], "READY")

    def test_ios_app_zip_can_register_but_not_execute(self):
        app_zip = self.root / "Demo.app.zip"
        with zipfile.ZipFile(app_zip, "w") as archive:
            archive.writestr("Payload/Demo.app/Info.plist", b"fixture")
        result = self.service.register_native_app(
            platform="ios",
            path=str(app_zip),
        )
        self.assertEqual(
            result["artifact"]["format"],
            "app_zip",
        )
        self.assertEqual(
            result["execution_support"]["ios"],
            "contract_only",
        )

    def test_changed_apk_fails_frozen_artifact_check(self):
        artifact = self.register()
        job = self.service.create_native_job(
            scenario=native_scenario(artifact["artifact_id"])
        )
        stored = self.service.store.get_native_app(
            artifact["artifact_id"]
        )
        Path(stored["path"]).write_bytes(b"changed")
        os.chmod(stored["path"], 0o600)
        with patch(
            "demo_video_mcp.service.native_runtime_status",
            return_value=ready_runtime(),
        ):
            result = self.service.preflight_job(job["job_id"])
        self.assertFalse(result["preflight"]["passed"])
        failed_names = {
            item["name"]
            for item in result["preflight"]["checks"]
            if item["status"] == "failed"
        }
        self.assertIn("app_artifact", failed_names)
        self.assertIn("frozen_plan", failed_names)

    def test_native_inspection_requires_explicit_launch_confirmation(self):
        artifact = self.register()
        with self.assertRaises(ValidationError):
            self.service.inspect_native_app(
                artifact_id=artifact["artifact_id"],
                package_id="com.example.demo",
                device={
                    "runtime": "emulator",
                    "udid": "emulator-5554",
                },
            )


if __name__ == "__main__":
    unittest.main()
