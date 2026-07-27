from __future__ import annotations

import unittest

from playwright.sync_api import sync_playwright

from demo_video_mcp.capture import resolve_capture


class CaptureTests(unittest.TestCase):
    def test_pixel_7_resolves_full_mobile_context(self):
        with sync_playwright() as playwright:
            context, metadata = resolve_capture(
                playwright,
                capture={
                    "target": "mobile",
                    "device": "pixel-7",
                    "orientation": "portrait",
                },
            )
        self.assertTrue(context["is_mobile"])
        self.assertTrue(context["has_touch"])
        self.assertIn("Mobile", context["user_agent"])
        self.assertEqual(metadata["viewport"], {"width": 412, "height": 839})
        self.assertEqual(metadata["record_size"], {"width": 412, "height": 839})
        self.assertEqual(metadata["output_size"], {"width": 1920, "height": 1080})

    def test_desktop_uses_standard_full_hd_viewport(self):
        with sync_playwright() as playwright:
            _, metadata = resolve_capture(
                playwright,
                legacy_viewport={"width": 1920, "height": 1080},
            )
        self.assertEqual(metadata["target"], "desktop")
        self.assertEqual(metadata["viewport"], {"width": 1920, "height": 1080})
        self.assertEqual(metadata["output_size"], {"width": 1920, "height": 1080})

    def test_desktop_rejects_non_standard_viewport(self):
        with sync_playwright() as playwright:
            with self.assertRaises(ValueError):
                resolve_capture(
                    playwright,
                    legacy_viewport={"width": 1280, "height": 720},
                )


if __name__ == "__main__":
    unittest.main()
