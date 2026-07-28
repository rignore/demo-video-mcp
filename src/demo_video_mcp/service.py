"""Application service used by the MCP protocol adapter."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from . import __version__
from .briefs import normalize_video_brief, validate_video_brief
from .captions import (
    caption_review_required,
    caption_storyboard,
    caption_summary,
)
from .capture import (
    STANDARD_OUTPUT_SIZE,
    normalize_capture,
    resolve_capture,
    validate_capture,
)
from .config import Settings
from .errors import ConflictError, NotFoundError, ValidationError
from .inspection import inspect_page
from .models import (
    canonical_hash,
    is_mutating_step,
    scenario_summary,
    url_origin,
    validate_scenario,
)
from .native_models import (
    native_scenario_summary,
    validate_native_scenario,
)
from .native_runtime import (
    inspect_android_app,
    native_runtime_status,
)
from .plugins import PluginRegistry, VideoPlugin
from .storage import (
    JobStore,
    allowed_path,
    atomic_write_json,
    read_json,
    utc_now,
    validate_id,
)


def _require_http_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a URL string")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(f"{label} must be an absolute http/https URL")
    try:
        parsed.port
    except ValueError as error:
        raise ValidationError(f"{label} has an invalid port") from error
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError(f"{label} must not contain URL credentials")
    return value


class VideoService:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings.from_env()
        self.settings.ensure_runtime_dirs()
        self.registry = PluginRegistry(self.settings.plugin_dirs)
        self.store = JobStore(self.settings.data_root)
        self._processes: Dict[int, subprocess.Popen[Any]] = {}

    def list_plugins(self) -> Dict[str, Any]:
        return {"plugins": self.registry.list()}

    def get_plugin(self, plugin_id: str) -> Dict[str, Any]:
        return {"plugin": self.registry.get(plugin_id).public_contract()}

    def scenario_schema(self) -> Dict[str, Any]:
        path = (
            Path(__file__).resolve().parent
            / "schemas"
            / "scenario-v1.json"
        )
        return {"schema": json.loads(path.read_text(encoding="utf-8"))}

    def plugin_schema(self) -> Dict[str, Any]:
        path = (
            Path(__file__).resolve().parent
            / "schemas"
            / "plugin-v1.json"
        )
        return {"schema": json.loads(path.read_text(encoding="utf-8"))}

    def brief_schema(self) -> Dict[str, Any]:
        path = (
            Path(__file__).resolve().parent
            / "schemas"
            / "video-brief-v1.json"
        )
        return {"schema": json.loads(path.read_text(encoding="utf-8"))}

    def native_scenario_schema(self) -> Dict[str, Any]:
        path = (
            Path(__file__).resolve().parent
            / "schemas"
            / "native-scenario-v1.json"
        )
        return {"schema": json.loads(path.read_text(encoding="utf-8"))}

    def app_artifact_schema(self) -> Dict[str, Any]:
        path = (
            Path(__file__).resolve().parent
            / "schemas"
            / "app-artifact-v1.json"
        )
        return {"schema": json.loads(path.read_text(encoding="utf-8"))}

    def register_native_app(
        self,
        *,
        platform: Any,
        path: Any,
    ) -> Dict[str, Any]:
        if platform not in {"android", "ios"}:
            raise ValidationError("platform must be android or ios")
        if not isinstance(path, str) or not path.strip():
            raise ValidationError("path must be a non-empty local path")
        source = Path(path).expanduser()
        if not source.is_absolute():
            raise ValidationError("path must be absolute")
        if source.is_symlink() or not source.is_file():
            raise ValidationError(
                "native app must be a regular local file, not a symlink"
            )
        size_bytes = source.stat().st_size
        if size_bytes <= 0:
            raise ValidationError("native app is empty")
        if size_bytes > self.settings.native_app_max_bytes:
            raise ValidationError(
                "native app exceeds DEMO_VIDEO_NATIVE_APP_MAX_BYTES"
            )
        lower_name = source.name.lower()
        if platform == "android":
            if not lower_name.endswith(".apk"):
                raise ValidationError("Android app artifact must be an APK")
            artifact_format = "apk"
        else:
            if not lower_name.endswith(".app.zip"):
                raise ValidationError(
                    "iOS Simulator artifact must be a .app.zip file"
                )
            artifact_format = "app_zip"
        if not zipfile.is_zipfile(source):
            raise ValidationError("native app artifact is not a valid ZIP")
        try:
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if len(members) > 100_000:
                    raise ValidationError(
                        "native app archive has too many entries"
                    )
                names = {member.filename for member in members}
        except (OSError, zipfile.BadZipFile) as error:
            raise ValidationError(
                "native app artifact cannot be inspected"
            ) from error
        if platform == "android" and "AndroidManifest.xml" not in names:
            raise ValidationError("APK is missing AndroidManifest.xml")
        if platform == "ios" and not any(
            name.endswith(".app/Info.plist") for name in names
        ):
            raise ValidationError(
                "iOS artifact is missing an app bundle Info.plist"
            )
        sha256 = self._file_sha256(source)
        if sha256 is None:
            raise ValidationError("native app artifact is unavailable")
        artifact = self.store.register_native_app(
            source=source,
            platform=platform,
            artifact_format=artifact_format,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        return {
            "artifact": {
                key: value
                for key, value in artifact.items()
                if key != "path"
            },
            "execution_support": {
                "android": "available",
                "ios": "contract_only",
            },
        }

    def list_native_apps(self) -> Dict[str, Any]:
        return {
            "artifacts": [
                {
                    key: value
                    for key, value in artifact.items()
                    if key != "path"
                }
                for artifact in self.store.list_native_apps()
            ]
        }

    def get_native_runtime_status(
        self,
        *,
        device: Any = None,
    ) -> Dict[str, Any]:
        if device is not None and not isinstance(device, dict):
            raise ValidationError("device must be an object")
        return native_runtime_status(self.settings, device)

    def read_guide(self, path: str) -> Dict[str, Any]:
        requested = allowed_path(
            Path(path),
            self.settings.allowed_guide_roots,
        )
        try:
            requested.relative_to(self.settings.data_root.resolve())
        except ValueError:
            pass
        else:
            raise ValidationError(
                "runtime profiles, jobs, and auth sessions cannot be read "
                "as guides"
            )
        hidden_parts = [
            part for part in requested.parts if part.startswith(".")
        ]
        if hidden_parts:
            raise ValidationError(
                "guides cannot be read from hidden files or directories"
            )
        if not requested.is_file():
            raise NotFoundError(f"guide file not found: {requested}")
        if requested.suffix.lower() not in {
            ".md",
            ".txt",
            ".json",
            ".yaml",
            ".yml",
        }:
            raise ValidationError(
                "guide must be Markdown, text, JSON, or YAML"
            )
        size = requested.stat().st_size
        if size > 1_000_000:
            raise ValidationError("guide exceeds 1 MB")
        content = requested.read_text(encoding="utf-8")
        return {
            "path": str(requested),
            "sha256": hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),
            "content": content,
        }

    @staticmethod
    def _normalize_brief(brief: Any) -> Optional[Dict[str, Any]]:
        if brief is None:
            return None
        errors = validate_video_brief(brief)
        if errors:
            raise ValidationError(
                "video brief validation failed:\n- "
                + "\n- ".join(errors)
            )
        try:
            return normalize_video_brief(brief)
        except ValueError as error:
            raise ValidationError(str(error)) from error

    @staticmethod
    def _normalize_capture(capture: Any) -> Dict[str, Any]:
        errors = validate_capture(capture)
        if errors:
            raise ValidationError(
                "capture validation failed:\n- " + "\n- ".join(errors)
            )
        try:
            return normalize_capture(capture)
        except ValueError as error:
            raise ValidationError(str(error)) from error

    @staticmethod
    def _validate_plugin_origin(
        plugin: VideoPlugin,
        url: str,
    ) -> str:
        origin = url_origin(url)
        allowed = set(plugin.allowed_origins)
        if "*" not in allowed and origin not in {
            url_origin(item) for item in allowed
        }:
            raise ValidationError(
                f"URL origin is not allowed by plugin: {origin}"
            )
        return origin

    def get_planning_context(
        self,
        *,
        plugin_id: str,
        brief: Any,
        start_url: Any = None,
        profile_id: Any = None,
        capture: Any = None,
        guide_paths: Any = None,
    ) -> Dict[str, Any]:
        plugin = self.registry.get(plugin_id)
        normalized_brief = self._normalize_brief(brief)
        if normalized_brief is None:
            raise ValidationError("brief is required")
        normalized_capture = self._normalize_capture(capture)

        missing_inputs: List[str] = []
        normalized_start_url = None
        if start_url is None:
            missing_inputs.append("start_url")
        else:
            normalized_start_url = _require_http_url(
                start_url,
                "start_url",
            )
            self._validate_plugin_origin(plugin, normalized_start_url)

        profile_summary = None
        if profile_id is not None:
            validate_id(profile_id, "profile_id")
            try:
                profile = self.store.get_profile(profile_id)
            except NotFoundError:
                missing_inputs.append("login_profile")
            else:
                profile_summary = {
                    "profile_id": profile_id,
                    "origin": profile.get("origin"),
                    "auth_state_available": profile.get(
                        "auth_state_available",
                        False,
                    ),
                }
                if not profile_summary["auth_state_available"]:
                    missing_inputs.append("login_profile")
                if (
                    normalized_start_url
                    and profile.get("origin")
                    != url_origin(normalized_start_url)
                ):
                    missing_inputs.append("profile_for_start_url_origin")

        requested_paths = guide_paths or []
        if not isinstance(requested_paths, list) or not all(
            isinstance(item, str) for item in requested_paths
        ):
            raise ValidationError("guide_paths must be an array of strings")
        plugin_contract = plugin.public_contract()
        paths = [
            item["path"] for item in plugin_contract.get("guides", [])
        ]
        for path in requested_paths:
            if path not in paths:
                paths.append(path)
        guides = [self.read_guide(path) for path in paths]

        duration = normalized_brief["duration_seconds"]
        suggested_scene_count = max(2, min(12, round(duration / 8)))
        scenario_seed: Dict[str, Any] = {
            "schema_version": 1,
            "title": normalized_brief["purpose"][:120],
            "capture": normalized_capture,
            "steps": [],
        }
        if normalized_start_url:
            scenario_seed["start_url"] = normalized_start_url
            scenario_seed["allowed_origins"] = [
                url_origin(normalized_start_url)
            ]
        return {
            "brief": normalized_brief,
            "brief_hash": canonical_hash(normalized_brief),
            "plugin": plugin_contract,
            "profile": profile_summary,
            "capture": normalized_capture,
            "delivery": {
                "format": "mp4",
                "output_size": dict(STANDARD_OUTPUT_SIZE),
            },
            "scenario_schema": self.scenario_schema()["schema"],
            "scenario_seed": scenario_seed,
            "guides": guides,
            "suggested_scene_count": suggested_scene_count,
            "caption_planning": {
                "decision_required": True,
                "required_when": [
                    "The audience needs guidance to understand the screen.",
                    "The flow is for onboarding, training, or external delivery.",
                    "Multiple UI transitions need an explanatory narrative.",
                    "A visible action has a purpose that the UI does not explain.",
                ],
                "not_required_when": [
                    "The video is short internal evidence of a defect.",
                    "The visible UI and actions are self-explanatory.",
                ],
                "scenario_contract": {
                    "captions": {
                        "decision": "required | not_required",
                        "reason": "Why the full flow does or does not need captions",
                        "language": "ko-KR",
                        "output": "sidecar | burned_in | both",
                    },
                    "step_caption": {
                        "screen": "Human-readable screen name",
                        "text": "Caption visible during this scene",
                    },
                    "minimum_captioned_hold_ms": 1200,
                },
                "approval": (
                    "When captions are required, preflight exposes the exact "
                    "screen-to-caption storyboard and the user must approve "
                    "the frozen plan before recording."
                ),
            },
            "authoring_rules": [
                "The host model authors the Scenario V1; the MCP server does "
                "not call an embedded LLM.",
                "Map every key message to at least one visible scene.",
                "Use the same capture settings for inspection and recording.",
                "Every final MP4 is 1920x1080. Mobile/tablet scenes keep "
                "their device viewport and are centered on that canvas.",
                "Do not invent controls or locators that are absent from a "
                "guide, template, or inspect_video_site result.",
                "Prefer role/name locators and add a completion assertion "
                "after navigation or interaction.",
                "Do not add external data changes unless the brief explicitly "
                "requires them; all potential mutations still require exact "
                "plan approval.",
                "Evaluate the complete user flow and add captions.decision "
                "with a concrete reason. When captions are required, add "
                "screen and text to every meaningful captioned scene.",
                "Before approval, show the user every screen-to-caption entry "
                "returned by preflight. Do not record until the user confirms "
                "that frozen storyboard.",
                f"Target approximately {duration} seconds across "
                f"{suggested_scene_count} scenes.",
            ],
            "missing_inputs": sorted(set(missing_inputs)),
        }

    def inspect_site(
        self,
        *,
        plugin_id: str,
        start_url: str,
        profile_id: Any = None,
        capture: Any = None,
        maximum_items: Any = 80,
        wait_ms: Any = 1200,
        include_screenshot: Any = False,
    ) -> Dict[str, Any]:
        plugin = self.registry.get(plugin_id)
        start_url = _require_http_url(start_url, "start_url")
        start_origin = self._validate_plugin_origin(plugin, start_url)
        normalized_capture = self._normalize_capture(capture)
        if (
            not isinstance(maximum_items, int)
            or isinstance(maximum_items, bool)
            or not 1 <= maximum_items <= 200
        ):
            raise ValidationError(
                "maximum_items must be an integer between 1 and 200"
            )
        if (
            not isinstance(wait_ms, int)
            or isinstance(wait_ms, bool)
            or not 0 <= wait_ms <= 5000
        ):
            raise ValidationError(
                "wait_ms must be an integer between 0 and 5000"
            )
        if not isinstance(include_screenshot, bool):
            raise ValidationError("include_screenshot must be a boolean")

        storage_state_path = None
        if profile_id is not None:
            validate_id(profile_id, "profile_id")
            profile = self.store.get_profile(profile_id)
            if not profile.get("auth_state_available"):
                raise ValidationError("profile has no storage state")
            if profile.get("origin") != start_origin:
                raise ValidationError(
                    "profile origin does not match inspection start_url"
                )
            storage_state_path = (
                self.store.profile_dir(profile_id) / "state.json"
            )
        allowed_origins = (
            ["*"]
            if "*" in plugin.allowed_origins
            else [url_origin(item) for item in plugin.allowed_origins]
        )
        try:
            return inspect_page(
                data_root=self.settings.data_root,
                start_url=start_url,
                allowed_origins=allowed_origins,
                storage_state_path=storage_state_path,
                capture=normalized_capture,
                maximum_items=maximum_items,
                wait_ms=wait_ms,
                include_screenshot=include_screenshot,
            )
        except ValidationError:
            raise
        except Exception as error:
            raise ValidationError(
                f"site inspection failed: {error}"
            ) from error

    def _resolve_scenario(
        self,
        plugin: VideoPlugin,
        scenario: Any,
        template_id: Any,
        variables: Any,
    ) -> Dict[str, Any]:
        if scenario is not None and template_id is not None:
            raise ValidationError(
                "provide either scenario or template_id, not both"
            )
        if template_id is not None:
            if not isinstance(template_id, str):
                raise ValidationError("template_id must be a string")
            if variables is None:
                variables = {}
            if not isinstance(variables, dict):
                raise ValidationError("variables must be an object")
            return plugin.load_template(template_id, variables)
        if not isinstance(scenario, dict):
            raise ValidationError(
                "scenario object or template_id is required"
            )
        return dict(scenario)

    @staticmethod
    def _file_sha256(path: Path) -> Optional[str]:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _profile_fingerprint(self, profile_id: Any) -> Optional[str]:
        if profile_id is None:
            return None
        validate_id(profile_id, "profile_id")
        return self._file_sha256(
            self.store.profile_dir(profile_id) / "state.json"
        )

    @staticmethod
    def _plan_hash(
        *,
        scenario: Mapping[str, Any],
        plugin_id: str,
        plugin_version: str,
        plugin_digest: str,
        profile_id: Any,
        profile_fingerprint: Any,
        guide_sources: Any,
        brief: Any,
        server_version: str,
    ) -> str:
        return canonical_hash(
            {
                "scenario": scenario,
                "plugin_id": plugin_id,
                "plugin_version": plugin_version,
                "plugin_digest": plugin_digest,
                "profile_id": profile_id,
                "profile_fingerprint": profile_fingerprint,
                "guide_sources": guide_sources,
                "brief": brief,
                "server_version": server_version,
            }
        )

    def _current_plan_hash(
        self,
        job_id: str,
        plugin: VideoPlugin,
        request: Mapping[str, Any],
    ) -> str:
        return self._plan_hash(
            scenario=self.store.get_scenario(job_id),
            plugin_id=plugin.plugin_id,
            plugin_version=plugin.manifest["version"],
            plugin_digest=plugin.digest,
            profile_id=request.get("profile_id"),
            profile_fingerprint=self._profile_fingerprint(
                request.get("profile_id")
            ),
            guide_sources=request.get("guide_sources", []),
            brief=request.get("brief"),
            server_version=__version__,
        )

    def _validated_native_artifact(
        self,
        scenario: Mapping[str, Any],
    ) -> Dict[str, Any]:
        artifact_id = scenario.get("app_artifact_id")
        if not isinstance(artifact_id, str):
            raise ValidationError("app_artifact_id is required")
        artifact = self.store.get_native_app(artifact_id)
        if artifact.get("artifact_id") != artifact_id:
            raise ConflictError("native app artifact metadata is inconsistent")
        if artifact.get("platform") != scenario.get("platform"):
            raise ValidationError(
                "app artifact platform does not match native scenario"
            )
        if artifact.get("format") != "apk":
            raise ValidationError(
                "only Android APK execution is currently available"
            )
        path = Path(artifact["path"])
        actual_size = path.stat().st_size
        actual_sha256 = self._file_sha256(path)
        if (
            actual_size != artifact.get("size_bytes")
            or actual_sha256 != artifact.get("sha256")
        ):
            raise ConflictError(
                "registered app artifact changed; register it again"
            )
        return artifact

    @staticmethod
    def _native_plan_hash(
        *,
        scenario: Mapping[str, Any],
        artifact: Mapping[str, Any],
        server_version: str,
    ) -> str:
        return canonical_hash(
            {
                "scenario": scenario,
                "backend": "native-android",
                "artifact": {
                    "artifact_id": artifact.get("artifact_id"),
                    "platform": artifact.get("platform"),
                    "format": artifact.get("format"),
                    "sha256": artifact.get("sha256"),
                    "size_bytes": artifact.get("size_bytes"),
                },
                "server_version": server_version,
            }
        )

    def _current_native_plan_hash(
        self,
        job_id: str,
    ) -> str:
        scenario = self.store.get_scenario(job_id)
        artifact = self._validated_native_artifact(scenario)
        return self._native_plan_hash(
            scenario=scenario,
            artifact=artifact,
            server_version=__version__,
        )

    def create_native_job(self, *, scenario: Any) -> Dict[str, Any]:
        errors = validate_native_scenario(scenario)
        if errors:
            raise ValidationError(
                "native scenario validation failed:\n- "
                + "\n- ".join(errors)
            )
        resolved_scenario = dict(scenario)
        artifact = self._validated_native_artifact(resolved_scenario)
        plan_hash = self._native_plan_hash(
            scenario=resolved_scenario,
            artifact=artifact,
            server_version=__version__,
        )
        request = {
            "backend": "native-android",
            "server_version": __version__,
            "app_artifact_id": artifact["artifact_id"],
            "artifact_sha256": artifact["sha256"],
            "artifact_size_bytes": artifact["size_bytes"],
        }
        status = self.store.create_job(
            plugin_id="native-android",
            scenario=resolved_scenario,
            request=request,
            plan_hash=plan_hash,
            summary=native_scenario_summary(resolved_scenario),
        )
        return self.get_job(status["job_id"])

    def inspect_native_app(
        self,
        *,
        artifact_id: Any,
        package_id: Any,
        device: Any,
        reset_policy: Any = "preserve",
        include_text: Any = False,
        maximum_items: Any = 80,
        include_screenshot: Any = False,
        confirm_app_launch: Any = False,
    ) -> Dict[str, Any]:
        scenario = {
            "schema_version": 1,
            "title": "Native app inspection",
            "platform": "android",
            "app_artifact_id": artifact_id,
            "package_id": package_id,
            "device": device,
            "reset_policy": reset_policy,
            "steps": [
                {
                    "id": "launch",
                    "title": "Launch app for inspection",
                    "action": {"type": "launch"},
                    "effects": ["potential_mutation"],
                    "approval": "required",
                    "retry_policy": "never",
                }
            ],
        }
        errors = validate_native_scenario(scenario)
        if errors:
            raise ValidationError(
                "native inspection validation failed:\n- "
                + "\n- ".join(errors)
            )
        if not isinstance(include_text, bool):
            raise ValidationError("include_text must be boolean")
        if not isinstance(include_screenshot, bool):
            raise ValidationError("include_screenshot must be boolean")
        if confirm_app_launch is not True:
            raise ValidationError(
                "confirm_app_launch must be true after the user accepts "
                "that installing and launching the app can trigger network "
                "effects"
            )
        if (
            not isinstance(maximum_items, int)
            or isinstance(maximum_items, bool)
            or not 1 <= maximum_items <= 200
        ):
            raise ValidationError("maximum_items must be an integer 1..200")
        artifact = self._validated_native_artifact(scenario)
        runtime = native_runtime_status(self.settings, device)
        if not runtime["ready"]:
            failed = [
                detail
                for check in runtime["checks"]
                if check["status"] == "failed"
                for detail in check["details"]
            ]
            raise ValidationError(
                "native runtime is not ready:\n- " + "\n- ".join(failed)
            )
        try:
            result = inspect_android_app(
                self.settings,
                scenario=scenario,
                app_path=Path(artifact["path"]),
                include_text=include_text,
                maximum_items=maximum_items,
                include_screenshot=include_screenshot,
            )
        except ValidationError:
            raise
        except Exception as error:
            raise ValidationError(
                f"native app inspection failed: {error}"
            ) from error
        return {
            **result,
            "artifact": {
                "artifact_id": artifact["artifact_id"],
                "sha256": artifact["sha256"],
            },
        }

    def create_job(
        self,
        *,
        plugin_id: str,
        scenario: Any = None,
        template_id: Any = None,
        variables: Any = None,
        profile_id: Any = None,
        guide_paths: Any = None,
        brief: Any = None,
        capture: Any = None,
    ) -> Dict[str, Any]:
        plugin = self.registry.get(plugin_id)
        resolved_scenario = self._resolve_scenario(
            plugin,
            scenario,
            template_id,
            variables,
        )
        scenario_profile_id = resolved_scenario.get("profile_id")
        if scenario_profile_id is not None:
            validate_id(scenario_profile_id, "scenario.profile_id")
            if (
                profile_id is not None
                and profile_id != scenario_profile_id
            ):
                raise ValidationError(
                    "scenario.profile_id and tool profile_id must match"
                )
            profile_id = scenario_profile_id
        if capture is not None:
            resolved_scenario["capture"] = self._normalize_capture(capture)
        normalized_brief = self._normalize_brief(brief)
        errors = validate_scenario(
            resolved_scenario,
            plugin_actions=plugin.supported_actions,
            plugin_allowed_origins=plugin.allowed_origins,
            allow_file_urls=self.settings.allow_file_urls,
        )
        if errors:
            raise ValidationError(
                "scenario validation failed:\n- " + "\n- ".join(errors)
            )

        if profile_id is not None:
            validate_id(profile_id, "profile_id")
        paths = guide_paths or []
        if not isinstance(paths, list) or not all(
            isinstance(item, str) for item in paths
        ):
            raise ValidationError("guide_paths must be an array of strings")
        guide_records = []
        for path in paths:
            guide = self.read_guide(path)
            guide_records.append(
                {"path": guide["path"], "sha256": guide["sha256"]}
            )

        profile_fingerprint = self._profile_fingerprint(profile_id)
        request = {
            "plugin_id": plugin_id,
            "server_version": __version__,
            "plugin_version": plugin.manifest["version"],
            "plugin_digest": plugin.digest,
            "profile_id": profile_id,
            "profile_fingerprint": profile_fingerprint,
            "guide_sources": guide_records,
            "template_id": template_id,
            "variables": variables or {},
            "brief": normalized_brief,
            "capture_override": (
                resolved_scenario.get("capture")
                if capture is not None
                else None
            ),
        }
        plan_hash = self._plan_hash(
            scenario=resolved_scenario,
            plugin_id=plugin_id,
            plugin_version=plugin.manifest["version"],
            plugin_digest=plugin.digest,
            profile_id=profile_id,
            profile_fingerprint=profile_fingerprint,
            guide_sources=guide_records,
            brief=normalized_brief,
            server_version=__version__,
        )
        status = self.store.create_job(
            plugin_id=plugin_id,
            scenario=resolved_scenario,
            request=request,
            plan_hash=plan_hash,
            summary=scenario_summary(resolved_scenario),
        )

        job_dir = self.store.job_dir(status["job_id"])
        if normalized_brief is not None:
            atomic_write_json(job_dir / "brief.json", normalized_brief)
        for index, path in enumerate(paths):
            guide = self.read_guide(path)
            source_path = Path(guide["path"])
            destination = (
                job_dir
                / "input"
                / "guides"
                / f"{index + 1:02d}-{source_path.name}"
            )
            destination.write_text(guide["content"], encoding="utf-8")

        return self.get_job(status["job_id"])

    def _preflight_native_job(
        self,
        job_id: str,
        status: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        existing_preflight = self.store.job_dir(job_id) / "preflight.json"
        if status["state"] in {"READY", "AWAITING_APPROVAL"}:
            if (
                existing_preflight.is_file()
                and self._current_native_plan_hash(job_id)
                == status["plan_hash"]
            ):
                return {
                    "job": self.get_job(job_id),
                    "preflight": read_json(existing_preflight),
                }
            raise ConflictError(
                "frozen native plan changed after preflight; "
                "create a new job"
            )
        if status["state"] not in {
            "DRAFT",
            "NEEDS_INPUT",
            "PREFLIGHT_FAILED",
        }:
            raise ConflictError(
                f"preflight is not allowed from state {status['state']}"
            )
        scenario = self.store.get_scenario(job_id)
        checks: List[Dict[str, Any]] = []
        errors = validate_native_scenario(scenario)
        checks.append(
            {
                "name": "native_scenario_schema",
                "status": "passed" if not errors else "failed",
                "details": errors,
            }
        )

        artifact_ok = False
        artifact_details: List[str] = []
        try:
            artifact = self._validated_native_artifact(scenario)
            artifact_ok = (
                artifact.get("sha256")
                == request.get("artifact_sha256")
                and artifact.get("size_bytes")
                == request.get("artifact_size_bytes")
            )
            if not artifact_ok:
                artifact_details.append(
                    "artifact metadata differs from the frozen request"
                )
        except (NotFoundError, ValidationError, ConflictError) as error:
            artifact_details.append(str(error))
        checks.append(
            {
                "name": "app_artifact",
                "status": "passed" if artifact_ok else "failed",
                "details": artifact_details,
            }
        )

        plan_unchanged = False
        if artifact_ok:
            try:
                plan_unchanged = (
                    self._current_native_plan_hash(job_id)
                    == status["plan_hash"]
                )
            except (NotFoundError, ValidationError, ConflictError):
                plan_unchanged = False
        checks.append(
            {
                "name": "frozen_plan",
                "status": "passed" if plan_unchanged else "failed",
                "details": (
                    []
                    if plan_unchanged
                    else [
                        "scenario, app artifact, or server version changed"
                    ]
                ),
            }
        )

        runtime = native_runtime_status(
            self.settings,
            scenario.get("device")
            if isinstance(scenario.get("device"), dict)
            else None,
        )
        checks.extend(runtime["checks"])
        failed = [item for item in checks if item["status"] == "failed"]
        steps = scenario.get("steps", [])
        mutations = [
            {
                "step_id": step["id"],
                "title": step["title"],
                "effects": step["effects"],
                "retry_policy": step["retry_policy"],
            }
            for step in steps
            if isinstance(step, dict) and is_mutating_step(step)
        ]
        caption_review = {
            **caption_summary(scenario),
            "required": caption_review_required(scenario),
            "storyboard": caption_storyboard(scenario),
        }
        requires_approval = bool(
            mutations or caption_review["required"]
        )
        preflight = {
            "job_id": job_id,
            "backend": "native-android",
            "plan_hash": status["plan_hash"],
            "checks": checks,
            "runtime": runtime,
            "mutations": mutations,
            "caption_review": caption_review,
            "requires_approval": requires_approval,
            "passed": not failed,
            "created_at": utc_now(),
        }
        atomic_write_json(
            self.store.job_dir(job_id) / "preflight.json",
            preflight,
        )
        if failed:
            next_state = "PREFLIGHT_FAILED"
        elif requires_approval:
            next_state = "AWAITING_APPROVAL"
        else:
            next_state = "READY"
        self.store.update_status(job_id, state=next_state, error=None)
        self.store.append_event(
            job_id,
            "native_preflight_completed",
            {"passed": not failed, "next_state": next_state},
        )
        return {
            "job": self.get_job(job_id),
            "preflight": preflight,
        }

    def preflight_job(self, job_id: str) -> Dict[str, Any]:
        status = self.store.get_status(job_id)
        request = read_json(self.store.job_dir(job_id) / "request.json")
        if request.get("backend") == "native-android":
            return self._preflight_native_job(job_id, status, request)
        existing_preflight = self.store.job_dir(job_id) / "preflight.json"
        if status["state"] in {"READY", "AWAITING_APPROVAL"}:
            plugin = self.registry.get(status["plugin_id"])
            if (
                existing_preflight.is_file()
                and self._current_plan_hash(job_id, plugin, request)
                == status["plan_hash"]
            ):
                return {
                    "job": self.get_job(job_id),
                    "preflight": read_json(existing_preflight),
                }
            raise ConflictError(
                "frozen plan changed after preflight; create a new job"
            )
        if status["state"] not in {
            "DRAFT",
            "NEEDS_INPUT",
            "PREFLIGHT_FAILED",
        }:
            raise ConflictError(
                f"preflight is not allowed from state {status['state']}"
            )
        scenario = self.store.get_scenario(job_id)
        plugin = self.registry.get(status["plugin_id"])
        checks: List[Dict[str, Any]] = []

        errors = validate_scenario(
            scenario,
            plugin_actions=plugin.supported_actions,
            plugin_allowed_origins=plugin.allowed_origins,
            allow_file_urls=self.settings.allow_file_urls,
        )
        checks.append(
            {
                "name": "scenario_schema",
                "status": "passed" if not errors else "failed",
                "details": errors,
            }
        )

        current_hash = self._current_plan_hash(
            job_id,
            plugin,
            request,
        )
        plan_unchanged = current_hash == status["plan_hash"]
        checks.append(
            {
                "name": "frozen_plan",
                "status": "passed" if plan_unchanged else "failed",
                "details": [] if plan_unchanged else [
                    "scenario, plugin, guide, brief, auth profile, or server "
                    "version changed"
                ],
            }
        )

        try:
            import playwright.sync_api  # noqa: F401

            playwright_available = True
        except ImportError:
            playwright_available = False
        checks.append(
            {
                "name": "playwright_runtime",
                "status": "passed" if playwright_available else "failed",
                "details": [] if playwright_available else [
                    "Playwright is not available in the MCP runtime"
                ],
            }
        )
        capture_details: List[str] = []
        capture_ok = playwright_available
        if playwright_available:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    _, resolved_capture = resolve_capture(
                        playwright,
                        capture=scenario.get("capture"),
                        legacy_viewport=scenario.get("viewport"),
                    )
                capture_details.append(
                    json.dumps(
                        resolved_capture,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            except Exception as error:
                capture_ok = False
                capture_details = [str(error)]
        else:
            capture_details = [
                "capture profile cannot be resolved without Playwright"
            ]
        checks.append(
            {
                "name": "capture_profile",
                "status": "passed" if capture_ok else "failed",
                "details": capture_details,
            }
        )

        profile_id = request.get("profile_id")
        if profile_id:
            try:
                profile = self.store.get_profile(profile_id)
                profile_origin = profile.get("origin")
                allowed_origins = {
                    url_origin(item)
                    for item in scenario["allowed_origins"]
                }
                profile_ok = bool(profile.get("auth_state_available"))
                profile_detail = []
                if not profile_ok:
                    profile_detail.append("profile has no storage state")
                if profile_origin not in allowed_origins:
                    profile_ok = False
                    profile_detail.append(
                        "profile origin does not match scenario origins"
                    )
            except (NotFoundError, ValidationError):
                profile_ok = False
                profile_detail = ["profile does not exist"]
            checks.append(
                {
                    "name": "auth_profile",
                    "status": "passed" if profile_ok else "failed",
                    "details": profile_detail,
                }
            )

        failed = [item for item in checks if item["status"] == "failed"]
        mutations = [
            {
                "step_id": step["id"],
                "title": step["title"],
                "effects": step["effects"],
                "retry_policy": step["retry_policy"],
            }
            for step in scenario["steps"]
            if is_mutating_step(step)
        ]
        caption_review = {
            **caption_summary(scenario),
            "required": caption_review_required(scenario),
            "storyboard": caption_storyboard(scenario),
        }
        requires_approval = bool(
            mutations or caption_review["required"]
        )
        preflight = {
            "job_id": job_id,
            "plan_hash": status["plan_hash"],
            "checks": checks,
            "mutations": mutations,
            "caption_review": caption_review,
            "requires_approval": requires_approval,
            "passed": not failed,
            "created_at": utc_now(),
        }
        atomic_write_json(
            self.store.job_dir(job_id) / "preflight.json",
            preflight,
        )
        if failed:
            next_state = "PREFLIGHT_FAILED"
        elif requires_approval:
            next_state = "AWAITING_APPROVAL"
        else:
            next_state = "READY"
        self.store.update_status(job_id, state=next_state, error=None)
        self.store.append_event(
            job_id,
            "preflight_completed",
            {"passed": not failed, "next_state": next_state},
        )
        return {
            "job": self.get_job(job_id),
            "preflight": preflight,
        }

    def approve_job(
        self,
        job_id: str,
        plan_hash: str,
        approved_step_ids: Any,
        confirm_external_changes: bool,
    ) -> Dict[str, Any]:
        status = self.store.get_status(job_id)
        approval_path = self.store.job_dir(job_id) / "approval.json"
        if status["state"] == "READY" and approval_path.is_file():
            approval = read_json(approval_path)
            if (
                approval.get("plan_hash") == plan_hash
                and set(approval.get("approved_step_ids", []))
                == set(approved_step_ids or [])
                and confirm_external_changes is True
            ):
                return self.get_job(job_id)
        if status["state"] != "AWAITING_APPROVAL":
            raise ConflictError(
                f"approval is not allowed from state {status['state']}"
            )
        if plan_hash != status["plan_hash"]:
            raise ConflictError(
                "plan hash changed; run preflight and review the new plan"
            )
        if confirm_external_changes is not True:
            raise ValidationError(
                "confirm_external_changes must be true after explicit "
                "user approval of the frozen recording plan"
            )
        scenario = self.store.get_scenario(job_id)
        expected = [
            step["id"]
            for step in scenario["steps"]
            if is_mutating_step(step)
        ]
        if not isinstance(approved_step_ids, list) or set(
            approved_step_ids
        ) != set(expected):
            raise ValidationError(
                "approved_step_ids must exactly match mutation steps: "
                + ", ".join(expected)
            )
        approval = {
            "job_id": job_id,
            "plan_hash": plan_hash,
            "approved_step_ids": expected,
            "caption_review": {
                **caption_summary(scenario),
                "storyboard": caption_storyboard(scenario),
            },
            "confirmed_at": utc_now(),
        }
        atomic_write_json(approval_path, approval)
        self.store.update_status(
            job_id,
            state="READY",
            approved_at=approval["confirmed_at"],
        )
        self.store.append_event(
            job_id,
            "external_changes_approved",
            {
                "plan_hash": plan_hash,
                "approved_step_ids": expected,
                "caption_storyboard_approved": (
                    caption_review_required(scenario)
                ),
            },
        )
        return self.get_job(job_id)

    def _worker_environment(self) -> Dict[str, str]:
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1])
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_root + (os.pathsep + existing if existing else "")
        )
        environment["DEMO_VIDEO_PROJECT_ROOT"] = str(
            self.settings.project_root
        )
        environment["DEMO_VIDEO_DATA_ROOT"] = str(self.settings.data_root)
        environment["DEMO_VIDEO_PLUGIN_DIRS"] = os.pathsep.join(
            str(path) for path in self.settings.plugin_dirs
        )
        environment["DEMO_VIDEO_ALLOWED_ROOTS"] = os.pathsep.join(
            str(path) for path in self.settings.allowed_guide_roots
        )
        if self.settings.allow_file_urls:
            environment["DEMO_VIDEO_ALLOW_FILE_URLS"] = "1"
        return environment

    def _spawn_worker(self, module: str, arguments: List[str], log: Path) -> int:
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("ab", buffering=0)
        os.chmod(log, 0o600)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", module, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                cwd=str(self.settings.project_root),
                env=self._worker_environment(),
                start_new_session=True,
            )
            self._processes[process.pid] = process
        finally:
            handle.close()
        return process.pid

    def _reap_processes(self) -> None:
        finished = [
            pid
            for pid, process in self._processes.items()
            if process.poll() is not None
        ]
        for pid in finished:
            self._processes.pop(pid, None)

    def _reap_terminal_process(
        self,
        status: Mapping[str, Any],
        terminal_states: set[str],
    ) -> None:
        if status.get("state") not in terminal_states:
            return
        pid = status.get("worker_pid")
        process = self._processes.get(pid)
        if process is None:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return
        self._processes.pop(pid, None)

    @staticmethod
    def _pid_is_running(pid: Any) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def _acquire_recording_lock(self, job_id: str) -> None:
        lock_path = self.settings.data_root / "recording.lock"
        for _ in range(2):
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                active_job_id = lock_path.read_text(
                    encoding="utf-8"
                ).strip()
                try:
                    active = self.store.get_status(active_job_id)
                except (NotFoundError, ValidationError):
                    lock_path.unlink(missing_ok=True)
                    continue
                if active["state"] in {
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELLED",
                    "NEEDS_USER",
                } or not self._pid_is_running(active.get("worker_pid")):
                    if active["state"] not in {
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "NEEDS_USER",
                    }:
                        self.store.update_status(
                            active_job_id,
                            state="FAILED",
                            error="recording worker stopped unexpectedly",
                            finished_at=utc_now(),
                        )
                    lock_path.unlink(missing_ok=True)
                    continue
                raise ConflictError(
                    f"another recording is active: {active_job_id}"
                )
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(job_id)
                return
        raise ConflictError("could not acquire recording worker lock")

    def _release_recording_lock(self, job_id: str) -> None:
        lock_path = self.settings.data_root / "recording.lock"
        if not lock_path.is_file():
            return
        if lock_path.read_text(encoding="utf-8").strip() == job_id:
            lock_path.unlink()

    def _start_native_job(
        self,
        job_id: str,
        plan_hash: str,
        status: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if status["state"] != "READY":
            raise ConflictError(
                f"recording is not allowed from state {status['state']}"
            )
        if plan_hash != status["plan_hash"]:
            raise ConflictError(
                "plan hash changed; run preflight and approval again"
            )
        if self._current_native_plan_hash(job_id) != plan_hash:
            raise ConflictError(
                "native scenario, app artifact, or server version changed; "
                "create and approve a new job"
            )
        scenario = self.store.get_scenario(job_id)
        if (
            any(
                is_mutating_step(step)
                for step in scenario.get("steps", [])
                if isinstance(step, dict)
            )
            or caption_review_required(scenario)
        ):
            approval_path = self.store.job_dir(job_id) / "approval.json"
            if not approval_path.is_file():
                raise ConflictError("recording plan approval is missing")
            approval = read_json(approval_path)
            if approval.get("plan_hash") != plan_hash:
                raise ConflictError("recording plan approval is stale")
        self._acquire_recording_lock(job_id)
        self.store.update_status(
            job_id,
            state="QUEUED",
            error=None,
            worker_pid=os.getpid(),
        )
        try:
            pid = self._spawn_worker(
                "demo_video_mcp.native_worker",
                ["--job-id", job_id],
                self.store.job_dir(job_id) / "worker.log",
            )
        except Exception:
            self._release_recording_lock(job_id)
            self.store.update_status(job_id, state="READY")
            raise
        self.store.update_status(job_id, worker_pid=pid)
        self.store.append_event(
            job_id,
            "native_worker_started",
            {"pid": pid},
        )
        return self.get_job(job_id)

    def start_job(self, job_id: str, plan_hash: str) -> Dict[str, Any]:
        status = self.store.get_status(job_id)
        request = read_json(self.store.job_dir(job_id) / "request.json")
        if request.get("backend") == "native-android":
            return self._start_native_job(job_id, plan_hash, status)
        if status["state"] != "READY":
            raise ConflictError(
                f"recording is not allowed from state {status['state']}"
            )
        if plan_hash != status["plan_hash"]:
            raise ConflictError(
                "plan hash changed; run preflight and approval again"
            )
        plugin = self.registry.get(status["plugin_id"])
        if self._current_plan_hash(job_id, plugin, request) != plan_hash:
            raise ConflictError(
                "scenario, plugin, guide, brief, auth profile, or server "
                "version changed; "
                "create and approve a new job"
            )
        scenario = self.store.get_scenario(job_id)
        if (
            any(is_mutating_step(step) for step in scenario["steps"])
            or caption_review_required(scenario)
        ):
            approval_path = self.store.job_dir(job_id) / "approval.json"
            if not approval_path.is_file():
                raise ConflictError("recording plan approval is missing")
            approval = read_json(approval_path)
            if approval.get("plan_hash") != plan_hash:
                raise ConflictError("recording plan approval is stale")
        self._acquire_recording_lock(job_id)
        self.store.update_status(
            job_id,
            state="QUEUED",
            error=None,
            worker_pid=os.getpid(),
        )
        try:
            pid = self._spawn_worker(
                "demo_video_mcp.worker",
                ["--job-id", job_id],
                self.store.job_dir(job_id) / "worker.log",
            )
        except Exception:
            self._release_recording_lock(job_id)
            self.store.update_status(job_id, state="READY")
            raise
        self.store.update_status(job_id, worker_pid=pid)
        self.store.append_event(job_id, "worker_started", {"pid": pid})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        self._reap_processes()
        status = self.store.get_status(job_id)
        self._reap_terminal_process(
            status,
            {"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_USER"},
        )
        job_dir = self.store.job_dir(job_id)
        response: Dict[str, Any] = {
            **status,
            "artifacts": self.store.list_artifacts(job_id),
        }
        for name in ("brief.json", "preflight.json", "manifest.json"):
            path = job_dir / name
            if path.is_file():
                response[name.removesuffix(".json")] = read_json(path)
        return response

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        status = self.store.get_status(job_id)
        if status["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return self.get_job(job_id)
        if status["state"] not in {
            "QUEUED",
            "RECORDING",
            "FINALIZING",
        }:
            raise ConflictError(
                f"cancellation is not allowed from state {status['state']}"
            )
        (self.store.job_dir(job_id) / "cancel.request").touch()
        self.store.append_event(job_id, "cancellation_requested")
        return self.get_job(job_id)

    def list_artifacts(self, job_id: str) -> Dict[str, Any]:
        self.store.get_status(job_id)
        return {
            "job_id": job_id,
            "artifacts": self.store.list_artifacts(job_id),
        }

    def start_login(
        self,
        *,
        profile_id: str,
        login_url: str,
        timeout_seconds: int = 600,
        capture: Any = None,
    ) -> Dict[str, Any]:
        validate_id(profile_id, "profile_id")
        _require_http_url(login_url, "login_url")
        if not isinstance(timeout_seconds, int) or not 30 <= timeout_seconds <= 1800:
            raise ValidationError("timeout_seconds must be between 30 and 1800")
        normalized_capture = self._normalize_capture(capture)
        if (self.store.profile_dir(profile_id) / "state.json").exists():
            raise ConflictError(
                "profile already exists; delete it explicitly before replacing"
            )
        for status_path in self.store.auth_sessions_root.glob(
            "*/status.json"
        ):
            try:
                active = read_json(status_path)
            except (NotFoundError, ValidationError):
                continue
            if (
                active.get("profile_id") == profile_id
                and active.get("state") in {"STARTING", "BROWSER_OPEN"}
                and self._pid_is_running(active.get("worker_pid"))
            ):
                raise ConflictError(
                    "an active login session already uses this profile: "
                    f"{active.get('session_id')}"
                )
        status = self.store.create_auth_session(
            profile_id,
            login_url,
            normalized_capture,
        )
        session_id = status["session_id"]
        self.store.update_auth_status(
            session_id,
            worker_pid=os.getpid(),
        )
        try:
            pid = self._spawn_worker(
                "demo_video_mcp.auth_worker",
                [
                    "--session-id",
                    session_id,
                    "--timeout-seconds",
                    str(timeout_seconds),
                ],
                self.store.auth_session_dir(session_id) / "worker.log",
            )
        except Exception as error:
            self.store.update_auth_status(
                session_id,
                state="FAILED",
                error=str(error),
            )
            raise
        return self.store.update_auth_status(session_id, worker_pid=pid)

    def finish_login(self, session_id: str) -> Dict[str, Any]:
        status = self.store.get_auth_status(session_id)
        if status["state"] == "COMPLETED":
            return status
        if status["state"] not in {"STARTING", "BROWSER_OPEN"}:
            raise ConflictError(
                f"finish is not allowed from state {status['state']}"
            )
        (self.store.auth_session_dir(session_id) / "finish.request").touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self.store.get_auth_status(session_id)
            if status["state"] in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            }:
                break
            time.sleep(0.2)
        return status

    def get_login_status(self, session_id: str) -> Dict[str, Any]:
        self._reap_processes()
        status = self.store.get_auth_status(session_id)
        self._reap_terminal_process(
            status,
            {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"},
        )
        return status

    def cancel_login(self, session_id: str) -> Dict[str, Any]:
        status = self.store.get_auth_status(session_id)
        if status["state"] in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
        }:
            return status
        (self.store.auth_session_dir(session_id) / "cancel.request").touch()
        return status

    def delete_profile(self, profile_id: str) -> Dict[str, Any]:
        profile_dir = self.store.profile_dir(profile_id)
        if not profile_dir.exists():
            return {"profile_id": profile_id, "deleted": False}
        shutil.rmtree(profile_dir)
        return {"profile_id": profile_id, "deleted": True}
