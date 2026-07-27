from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from demo_video_mcp.errors import ValidationError
from demo_video_mcp.plugins import PluginRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PluginTests(unittest.TestCase):
    def test_discovers_generic_and_optional_plugins(self):
        registry = PluginRegistry([PROJECT_ROOT / "plugins"])
        ids = [item["id"] for item in registry.list()]
        self.assertEqual(ids, ["generic-web", "protectgo"])

    def test_renders_template_variables(self):
        plugin = PluginRegistry([PROJECT_ROOT / "plugins"]).get(
            "generic-web"
        )
        scenario = plugin.load_template(
            "read-only-tour",
            {
                "start_url": "https://example.com/dashboard",
                "allowed_origin": "https://example.com",
                "heading": "Dashboard",
            },
        )
        self.assertEqual(
            scenario["steps"][0]["action"]["target"]["name"],
            "Dashboard",
        )

    def test_rejects_plugin_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "bad"
            plugin_root.mkdir()
            manifest = {
                "schema_version": 1,
                "id": "bad",
                "display_name": "Bad",
                "version": "1",
                "guides": [
                    {
                        "id": "escape",
                        "title": "Escape",
                        "path": "../secret.md",
                    }
                ],
                "scenario_templates": [],
            }
            (plugin_root / "plugin.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                PluginRegistry([plugin_root])


if __name__ == "__main__":
    unittest.main()
