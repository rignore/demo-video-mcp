from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from demo_video_mcp.config import Settings
from demo_video_mcp.service import VideoService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class InspectionTests(unittest.TestCase):
    def setUp(self):
        fixture_root = PROJECT_ROOT / "tests" / "fixtures" / "demo-site"

        def handler(*args, **kwargs):
            return QuietHandler(
                *args,
                directory=str(fixture_root),
                **kwargs,
            )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
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
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def test_mobile_inventory_does_not_activate_controls_or_leak_values(self):
        result = self.service.inspect_site(
            plugin_id="generic-web",
            start_url=f"{self.origin}/?session=secret-query",
            capture={"target": "mobile", "device": "pixel-7"},
            wait_ms=0,
        )
        heading_text = {item["text"] for item in result["headings"]}
        button_text = {item["text"] for item in result["buttons"]}
        self.assertIn("Mobile navigation", heading_text)
        self.assertNotIn("Desktop navigation", heading_text)
        self.assertIn("Do not click", button_text)
        self.assertNotIn("Clicked", button_text)
        self.assertNotIn("secret-query", result["final_url"])
        self.assertNotIn("secret-input-value", repr(result))
        self.assertNotIn("do-not-return", repr(result))
        self.assertIsNone(result["screenshot_path"])
        self.assertTrue(result["untrusted_content"])

    def test_screenshot_requires_explicit_opt_in_and_is_private(self):
        result = self.service.inspect_site(
            plugin_id="generic-web",
            start_url=self.origin,
            include_screenshot=True,
            wait_ms=0,
        )
        screenshot = Path(result["screenshot_path"])
        self.assertTrue(screenshot.is_file())
        self.assertEqual(screenshot.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
