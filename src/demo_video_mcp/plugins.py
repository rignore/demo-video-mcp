"""Explicit plugin discovery, templates, and optional trusted runtime hooks."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .errors import NotFoundError, ValidationError
from .models import CORE_ACTIONS, IDENTIFIER_RE


VARIABLE_RE = re.compile(r"\$\{([a-zA-Z][a-zA-Z0-9_]*)\}")


def _contained_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise ValidationError(
            f"plugin path escapes plugin directory: {relative}"
        ) from error
    return candidate


def _render_value(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        return {
            key: _render_value(item, variables)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_render_value(item, variables) for item in value]
    if not isinstance(value, str):
        return value

    whole_match = VARIABLE_RE.fullmatch(value)
    if whole_match:
        name = whole_match.group(1)
        if name not in variables:
            raise ValidationError(f"missing template variable: {name}")
        return variables[name]

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise ValidationError(f"missing template variable: {name}")
        return str(variables[name])

    return VARIABLE_RE.sub(replace, value)


@dataclass(frozen=True)
class VideoPlugin:
    root: Path
    manifest: Dict[str, Any]

    @property
    def plugin_id(self) -> str:
        return str(self.manifest["id"])

    @property
    def supported_actions(self) -> List[str]:
        return list(self.manifest.get("plugin_actions", []))

    @property
    def allowed_origins(self) -> List[str]:
        return list(self.manifest.get("allowed_origins", []))

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = str(path.relative_to(self.root)).encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            content = path.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    def public_contract(self) -> Dict[str, Any]:
        templates = []
        for item in self.manifest.get("scenario_templates", []):
            templates.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "required_variables": item.get(
                        "required_variables", []
                    ),
                }
            )
        return {
            "id": self.plugin_id,
            "display_name": self.manifest["display_name"],
            "version": self.manifest["version"],
            "digest": self.digest,
            "description": self.manifest.get("description", ""),
            "allowed_origins": self.allowed_origins,
            "plugin_actions": self.supported_actions,
            "guides": [
                {
                    "id": guide.get("id"),
                    "title": guide.get("title"),
                    "path": str(_contained_path(self.root, guide["path"])),
                }
                for guide in self.manifest.get("guides", [])
            ],
            "scenario_templates": templates,
            "trusted_runtime": bool(self.manifest.get("runtime")),
        }

    def load_template(
        self, template_id: str, variables: Mapping[str, Any]
    ) -> Dict[str, Any]:
        template = next(
            (
                item
                for item in self.manifest.get("scenario_templates", [])
                if item.get("id") == template_id
            ),
            None,
        )
        if not template:
            raise NotFoundError(
                f"plugin {self.plugin_id!r} has no template {template_id!r}"
            )
        required = template.get("required_variables", [])
        missing = [
            name
            for name in required
            if name not in variables or variables[name] in (None, "")
        ]
        if missing:
            raise ValidationError(
                "missing template variables: " + ", ".join(missing)
            )
        path = _contained_path(self.root, template["path"])
        if not path.is_file():
            raise NotFoundError(f"scenario template does not exist: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        rendered = _render_value(raw, variables)
        if not isinstance(rendered, dict):
            raise ValidationError("scenario template must render to an object")
        return rendered

    def load_runtime(self) -> Optional[ModuleType]:
        runtime_path = self.manifest.get("runtime")
        if not runtime_path:
            return None
        path = _contained_path(self.root, runtime_path)
        if not path.is_file():
            raise NotFoundError(f"plugin runtime does not exist: {path}")
        module_name = f"demo_video_plugin_{self.plugin_id}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValidationError(f"cannot load plugin runtime: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class PluginRegistry:
    def __init__(self, plugin_dirs: Iterable[Path]):
        self._plugins: Dict[str, VideoPlugin] = {}
        for plugin_dir in plugin_dirs:
            self._scan(plugin_dir)

    def _scan(self, plugin_dir: Path) -> None:
        if not plugin_dir.is_dir():
            return
        candidates = []
        if (plugin_dir / "plugin.json").is_file():
            candidates.append(plugin_dir)
        candidates.extend(
            path
            for path in sorted(plugin_dir.iterdir())
            if path.is_dir() and (path / "plugin.json").is_file()
        )
        for root in candidates:
            manifest = json.loads(
                (root / "plugin.json").read_text(encoding="utf-8")
            )
            self._validate_manifest(root, manifest)
            plugin_id = manifest["id"]
            if plugin_id in self._plugins:
                raise ValidationError(f"duplicate plugin id: {plugin_id}")
            self._plugins[plugin_id] = VideoPlugin(root, manifest)

    @staticmethod
    def _validate_manifest(root: Path, manifest: Any) -> None:
        if not isinstance(manifest, dict):
            raise ValidationError(f"{root}: plugin manifest must be an object")
        if manifest.get("schema_version") != 1:
            raise ValidationError(
                f"{root}: plugin schema_version must be 1"
            )
        plugin_id = manifest.get("id")
        if not isinstance(plugin_id, str) or not IDENTIFIER_RE.fullmatch(
            plugin_id
        ):
            raise ValidationError(f"{root}: invalid plugin id")
        for key in ("display_name", "version"):
            if not isinstance(manifest.get(key), str) or not manifest[key]:
                raise ValidationError(f"{root}: {key} is required")
        actions = manifest.get("plugin_actions", [])
        if not isinstance(actions, list) or not all(
            isinstance(item, str) and item for item in actions
        ):
            raise ValidationError(f"{root}: plugin_actions must be strings")
        conflicts = CORE_ACTIONS.intersection(actions)
        if conflicts:
            raise ValidationError(
                f"{root}: plugin action conflicts with core: "
                + ", ".join(sorted(conflicts))
            )
        for collection, required_keys in (
            ("guides", {"id", "title", "path"}),
            (
                "scenario_templates",
                {"id", "title", "description", "path"},
            ),
        ):
            items = manifest.get(collection, [])
            if not isinstance(items, list):
                raise ValidationError(f"{root}: {collection} must be an array")
            seen = set()
            for item in items:
                if not isinstance(item, dict) or not required_keys.issubset(
                    item
                ):
                    raise ValidationError(
                        f"{root}: invalid item in {collection}"
                    )
                item_id = item["id"]
                if not isinstance(item_id, str) or not IDENTIFIER_RE.fullmatch(
                    item_id
                ):
                    raise ValidationError(
                        f"{root}: invalid {collection} id {item_id!r}"
                    )
                if item_id in seen:
                    raise ValidationError(
                        f"{root}: duplicate {collection} id {item_id}"
                    )
                seen.add(item_id)
                _contained_path(root, item["path"])

    def list(self) -> List[Dict[str, Any]]:
        return [
            plugin.public_contract()
            for plugin in sorted(
                self._plugins.values(), key=lambda item: item.plugin_id
            )
        ]

    def get(self, plugin_id: str) -> VideoPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as error:
            raise NotFoundError(f"video plugin not found: {plugin_id}") from error
