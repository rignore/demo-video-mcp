from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from demo_video_mcp.config import Settings
from demo_video_mcp.media import (
    find_ffmpeg,
    probe_video_size,
)
from demo_video_mcp.native_worker import run_native_job
from demo_video_mcp.service import VideoService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ready_runtime(server_url: str) -> dict:
    checks = [
        {"name": name, "status": "passed", "details": []}
        for name in (
            "adb",
            "appium",
            "uiautomator2",
            "android_emulator",
            "ffmpeg",
        )
    ]
    return {
        "backend": "native-android",
        "ready": True,
        "appium_server_url": server_url,
        "appium_server_reachable": True,
        "appium_server_error": None,
        "connected_devices": ["emulator-5554"],
        "available_avds": [],
        "checks": checks,
    }


class NativeRecordingIntegrationTests(unittest.TestCase):
    def test_fake_appium_recording_becomes_full_hd_mp4(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_video = root / "source.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x640:d=2",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(source_video),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            encoded_video = base64.b64encode(
                source_video.read_bytes()
            ).decode("ascii")

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    return

                def _respond(self, value):
                    payload = json.dumps({"value": value}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def _consume(self):
                    length = int(self.headers.get("Content-Length", "0"))
                    if length:
                        self.rfile.read(length)

                def do_GET(self):
                    if self.path == "/status":
                        self._respond({"ready": True})
                    else:
                        self._respond({})

                def do_POST(self):
                    self._consume()
                    if self.path == "/session":
                        self._respond(
                            {
                                "sessionId": "fake-session",
                                "capabilities": {
                                    "platformName": "Android",
                                    "deviceName": "Fake Emulator",
                                },
                            }
                        )
                    elif self.path.endswith(
                        "/appium/stop_recording_screen"
                    ):
                        self._respond(encoded_video)
                    else:
                        self._respond(None)

                def do_DELETE(self):
                    self._consume()
                    self._respond(None)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            server_url = f"http://127.0.0.1:{server.server_port}"

            data_root = root / "data"
            settings = Settings(
                project_root=PROJECT_ROOT,
                data_root=data_root,
                plugin_dirs=(PROJECT_ROOT / "plugins",),
                allowed_guide_roots=(PROJECT_ROOT,),
                allow_file_urls=False,
                appium_server_url=server_url,
            )
            service = VideoService(settings)
            apk = root / "fixture.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"fixture")
                archive.writestr("classes.dex", b"fixture")
            artifact = service.register_native_app(
                platform="android",
                path=str(apk),
            )["artifact"]
            scenario = {
                "schema_version": 1,
                "title": "Fake native recording",
                "platform": "android",
                "app_artifact_id": artifact["artifact_id"],
                "package_id": "com.example.demo",
                "device": {
                    "runtime": "emulator",
                    "udid": "emulator-5554",
                    "orientation": "portrait",
                },
                "max_duration_seconds": 10,
                "captions": {
                    "decision": "required",
                    "reason": "네이티브 앱의 첫 화면을 안내합니다.",
                    "language": "ko-KR",
                    "output": "both",
                },
                "steps": [
                    {
                        "id": "launch",
                        "title": "Launch app",
                        "action": {"type": "launch"},
                        "effects": ["potential_mutation"],
                        "approval": "required",
                        "retry_policy": "never",
                        "hold_ms": 1200,
                        "caption": {
                            "screen": "앱 홈",
                            "text": "앱의 시작 화면을 확인합니다.",
                        },
                    }
                ],
            }
            job = service.create_native_job(scenario=scenario)
            with patch(
                "demo_video_mcp.service.native_runtime_status",
                return_value=ready_runtime(server_url),
            ):
                preflight = service.preflight_job(job["job_id"])
            self.assertTrue(preflight["preflight"]["passed"])
            service.approve_job(
                job["job_id"],
                job["plan_hash"],
                ["launch"],
                True,
            )
            service.store.update_status(job["job_id"], state="QUEUED")

            environment = {
                "DEMO_VIDEO_PROJECT_ROOT": str(PROJECT_ROOT),
                "DEMO_VIDEO_DATA_ROOT": str(data_root),
                "DEMO_VIDEO_PLUGIN_DIRS": str(PROJECT_ROOT / "plugins"),
                "DEMO_VIDEO_ALLOWED_ROOTS": str(PROJECT_ROOT),
                "DEMO_VIDEO_APPIUM_URL": server_url,
            }
            with patch.dict(os.environ, environment, clear=False):
                result = run_native_job(job["job_id"])
            self.assertEqual(result, 0)
            current = service.get_job(job["job_id"])
            self.assertEqual(
                current["state"],
                "SUCCEEDED",
                current.get("error"),
            )
            self.assertEqual(
                current["manifest"]["capture"]["output_size"],
                {"width": 1920, "height": 1080},
            )
            mp4 = next(
                Path(item["path"])
                for item in current["artifacts"]
                if item["artifact_id"] == "video.mp4"
            )
            self.assertEqual(
                probe_video_size(mp4),
                {"width": 1920, "height": 1080},
            )
            names = {
                item["artifact_id"] for item in current["artifacts"]
            }
            self.assertIn("captions.vtt", names)
            self.assertIn("video-captioned.mp4", names)
            captioned_mp4 = next(
                Path(item["path"])
                for item in current["artifacts"]
                if item["artifact_id"] == "video-captioned.mp4"
            )
            self.assertEqual(
                probe_video_size(captioned_mp4),
                {"width": 1920, "height": 1080},
            )


if __name__ == "__main__":
    unittest.main()
