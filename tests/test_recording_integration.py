from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from demo_video_mcp.config import Settings
from demo_video_mcp.media import probe_video_size
from demo_video_mcp.service import VideoService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class RecordingIntegrationTests(unittest.TestCase):
    def test_generic_template_records_mp4(self):
        fixture_root = PROJECT_ROOT / "tests" / "fixtures" / "demo-site"

        def handler(*args, **kwargs):
            return QuietHandler(*args, directory=str(fixture_root), **kwargs)

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(
                project_root=PROJECT_ROOT,
                data_root=Path(temporary) / "data",
                plugin_dirs=(PROJECT_ROOT / "plugins",),
                allowed_guide_roots=(PROJECT_ROOT,),
                allow_file_urls=False,
            )
            service = VideoService(settings)
            origin = f"http://127.0.0.1:{server.server_port}"
            job = service.create_job(
                plugin_id="generic-web",
                template_id="read-only-tour",
                variables={
                    "start_url": origin,
                    "allowed_origin": origin,
                    "heading": "Demo dashboard",
                },
            )
            preflight = service.preflight_job(job["job_id"])
            self.assertEqual(preflight["job"]["state"], "READY")
            service.start_job(job["job_id"], job["plan_hash"])

            deadline = time.monotonic() + 45
            current = None
            while time.monotonic() < deadline:
                current = service.get_job(job["job_id"])
                if current["state"] in {
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELLED",
                    "NEEDS_USER",
                }:
                    break
                time.sleep(0.2)
            self.assertIsNotNone(current)
            self.assertEqual(
                current["state"],
                "SUCCEEDED",
                current.get("error"),
            )
            names = {
                artifact["artifact_id"]
                for artifact in current["artifacts"]
            }
            self.assertIn("recording.webm", names)
            self.assertIn("video.mp4", names)
            mp4 = next(
                Path(artifact["path"])
                for artifact in current["artifacts"]
                if artifact["artifact_id"] == "video.mp4"
            )
            self.assertEqual(
                probe_video_size(mp4),
                {"width": 1920, "height": 1080},
            )

    def test_mobile_web_records_odd_height_pixel_mp4(self):
        fixture_root = PROJECT_ROOT / "tests" / "fixtures" / "demo-site"

        def handler(*args, **kwargs):
            return QuietHandler(*args, directory=str(fixture_root), **kwargs)

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(
                project_root=PROJECT_ROOT,
                data_root=Path(temporary) / "data",
                plugin_dirs=(PROJECT_ROOT / "plugins",),
                allowed_guide_roots=(PROJECT_ROOT,),
                allow_file_urls=False,
            )
            service = VideoService(settings)
            origin = f"http://127.0.0.1:{server.server_port}"
            scenario = {
                "schema_version": 1,
                "title": "Mobile fixture",
                "start_url": origin,
                "allowed_origins": [origin],
                "steps": [
                    {
                        "id": "verify-mobile-layout",
                        "title": "Verify mobile layout",
                        "action": {
                            "type": "wait_for",
                            "target": {
                                "by": "role",
                                "value": "heading",
                                "name": "Mobile navigation",
                                "exact": True,
                            },
                        },
                        "effects": ["remote_read"],
                        "approval": "none",
                        "retry_policy": "safe",
                        "hold_ms": 800,
                    }
                ],
            }
            job = service.create_job(
                plugin_id="generic-web",
                scenario=scenario,
                capture={
                    "target": "mobile",
                    "device": "pixel-7",
                    "orientation": "portrait",
                },
            )
            preflight = service.preflight_job(job["job_id"])
            self.assertEqual(preflight["job"]["state"], "READY")
            service.start_job(job["job_id"], job["plan_hash"])

            deadline = time.monotonic() + 45
            current = None
            while time.monotonic() < deadline:
                current = service.get_job(job["job_id"])
                if current["state"] in {
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELLED",
                    "NEEDS_USER",
                }:
                    break
                time.sleep(0.2)
            self.assertIsNotNone(current)
            self.assertEqual(
                current["state"],
                "SUCCEEDED",
                current.get("error"),
            )
            self.assertEqual(
                current["manifest"]["capture"]["viewport"],
                {"width": 412, "height": 839},
            )
            self.assertEqual(
                current["manifest"]["capture"]["output_size"],
                {"width": 1920, "height": 1080},
            )
            names = {
                artifact["artifact_id"]
                for artifact in current["artifacts"]
            }
            self.assertIn("recording.webm", names)
            self.assertIn("video.mp4", names)
            mp4 = next(
                Path(artifact["path"])
                for artifact in current["artifacts"]
                if artifact["artifact_id"] == "video.mp4"
            )
            self.assertEqual(
                probe_video_size(mp4),
                {"width": 1920, "height": 1080},
            )


if __name__ == "__main__":
    unittest.main()
