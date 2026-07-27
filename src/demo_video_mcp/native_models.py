"""Canonical Android native-app scenario validation."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

from .models import is_mutating_step


NATIVE_ACTIONS = {
    "launch",
    "wait_for",
    "tap",
    "fill",
    "press_key",
    "back",
    "swipe",
    "pause",
    "screenshot",
}
NATIVE_TARGET_KINDS = {
    "accessibility_id",
    "id",
    "text",
    "xpath",
    "class_name",
}
NATIVE_KEY_NAMES = {"BACK", "ENTER", "HOME", "TAB"}
NATIVE_ORIENTATIONS = {"portrait", "landscape"}
NATIVE_RESET_POLICIES = {"clean", "preserve"}
APPROVAL_VALUES = {"none", "required"}
RETRY_VALUES = {"safe", "never"}
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PACKAGE_ID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$"
)


def _string(
    errors: List[str],
    value: Any,
    location: str,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, str) or (
        not allow_empty and not value.strip()
    ):
        errors.append(f"{location}: non-empty string is required")


def _unexpected(
    errors: List[str],
    value: Mapping[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        errors.append(
            f"{location}: unsupported properties: {', '.join(extra)}"
        )


def _validate_target(
    errors: List[str],
    target: Any,
    location: str,
) -> None:
    if not isinstance(target, dict):
        errors.append(f"{location}: target object is required")
        return
    _unexpected(
        errors,
        target,
        {"by", "value", "nth"},
        location,
    )
    kind = target.get("by")
    if kind not in NATIVE_TARGET_KINDS:
        errors.append(
            f"{location}.by: expected one of "
            f"{sorted(NATIVE_TARGET_KINDS)}"
        )
    _string(errors, target.get("value"), f"{location}.value")
    if "nth" in target and (
        not isinstance(target["nth"], int) or target["nth"] < 0
    ):
        errors.append(f"{location}.nth: non-negative integer is required")


def validate_native_scenario(scenario: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(scenario, dict):
        return ["scenario: object is required"]
    _unexpected(
        errors,
        scenario,
        {
            "schema_version",
            "title",
            "platform",
            "app_artifact_id",
            "package_id",
            "device",
            "reset_policy",
            "max_duration_seconds",
            "steps",
        },
        "scenario",
    )
    if scenario.get("schema_version") != 1:
        errors.append("schema_version: expected 1")
    _string(errors, scenario.get("title"), "title")
    if scenario.get("platform") != "android":
        errors.append("platform: Android is the only executable backend")
    _string(errors, scenario.get("app_artifact_id"), "app_artifact_id")
    package_id = scenario.get("package_id")
    _string(errors, package_id, "package_id")
    if isinstance(package_id, str) and not PACKAGE_ID_RE.fullmatch(package_id):
        errors.append("package_id: expected an Android application ID")

    device = scenario.get("device")
    if not isinstance(device, dict):
        errors.append("device: object is required")
    else:
        _unexpected(
            errors,
            device,
            {
                "runtime",
                "avd",
                "udid",
                "device_name",
                "platform_version",
                "orientation",
                "language",
                "locale",
            },
            "device",
        )
        if device.get("runtime") != "emulator":
            errors.append("device.runtime: expected emulator")
        for field in (
            "avd",
            "udid",
            "device_name",
            "platform_version",
            "language",
            "locale",
        ):
            if field in device:
                _string(errors, device[field], f"device.{field}")
        udid = device.get("udid")
        if isinstance(udid, str) and not udid.startswith("emulator-"):
            errors.append(
                "device.udid: only Android Emulator serials are allowed"
            )
        if device.get("orientation", "portrait") not in NATIVE_ORIENTATIONS:
            errors.append(
                "device.orientation: expected portrait or landscape"
            )

    if scenario.get("reset_policy", "clean") not in NATIVE_RESET_POLICIES:
        errors.append(
            "reset_policy: expected clean or preserve"
        )
    duration = scenario.get("max_duration_seconds", 180)
    if (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or not 10 <= duration <= 180
    ):
        errors.append("max_duration_seconds: integer 10..180 required")

    steps = scenario.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps: non-empty array is required")
        return errors

    seen_ids = set()
    launch_count = 0
    for index, step in enumerate(steps):
        location = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{location}: object is required")
            continue
        _unexpected(
            errors,
            step,
            {
                "id",
                "title",
                "action",
                "effects",
                "approval",
                "retry_policy",
                "timeout_ms",
                "hold_ms",
            },
            location,
        )
        step_id = step.get("id")
        _string(errors, step_id, f"{location}.id")
        if isinstance(step_id, str):
            if not IDENTIFIER_RE.fullmatch(step_id):
                errors.append(
                    f"{location}.id: use lowercase letters, digits, _ or -"
                )
            if step_id in seen_ids:
                errors.append(f"{location}.id: duplicate {step_id!r}")
            seen_ids.add(step_id)
        _string(errors, step.get("title"), f"{location}.title")

        action = step.get("action")
        if not isinstance(action, dict):
            errors.append(f"{location}.action: object is required")
            continue
        action_type = action.get("type")
        if action_type not in NATIVE_ACTIONS:
            errors.append(
                f"{location}.action.type: unsupported action "
                f"{action_type!r}"
            )
            continue
        if action_type == "launch":
            launch_count += 1
            if index != 0:
                errors.append(
                    f"{location}.action.type: launch must be the first step"
                )
            _unexpected(errors, action, {"type"}, f"{location}.action")
        elif action_type in {"wait_for", "tap", "fill"}:
            allowed = {"type", "target"}
            if action_type == "wait_for":
                allowed.add("state")
            if action_type == "fill":
                allowed.add("value")
            _unexpected(errors, action, allowed, f"{location}.action")
            _validate_target(
                errors,
                action.get("target"),
                f"{location}.action.target",
            )
            if action_type == "fill":
                _string(
                    errors,
                    action.get("value"),
                    f"{location}.action.value",
                    allow_empty=True,
                )
            if (
                action_type == "wait_for"
                and action.get("state", "present")
                not in {"present", "absent"}
            ):
                errors.append(
                    f"{location}.action.state: expected present or absent"
                )
        elif action_type == "press_key":
            _unexpected(
                errors,
                action,
                {"type", "key"},
                f"{location}.action",
            )
            if action.get("key") not in NATIVE_KEY_NAMES:
                errors.append(
                    f"{location}.action.key: expected one of "
                    f"{sorted(NATIVE_KEY_NAMES)}"
                )
        elif action_type == "swipe":
            _unexpected(
                errors,
                action,
                {"type", "direction", "percent"},
                f"{location}.action",
            )
            if action.get("direction") not in {
                "up",
                "down",
                "left",
                "right",
            }:
                errors.append(
                    f"{location}.action.direction: unsupported direction"
                )
            percent = action.get("percent", 0.7)
            if (
                not isinstance(percent, (int, float))
                or isinstance(percent, bool)
                or not 0.1 <= float(percent) <= 1.0
            ):
                errors.append(
                    f"{location}.action.percent: number 0.1..1.0 required"
                )
        elif action_type == "pause":
            _unexpected(
                errors,
                action,
                {"type", "milliseconds"},
                f"{location}.action",
            )
            milliseconds = action.get("milliseconds")
            if (
                not isinstance(milliseconds, int)
                or isinstance(milliseconds, bool)
                or not 0 <= milliseconds <= 30_000
            ):
                errors.append(
                    f"{location}.action.milliseconds: 0..30000 required"
                )
        elif action_type == "screenshot":
            _unexpected(
                errors,
                action,
                {"type", "name"},
                f"{location}.action",
            )
            if "name" in action:
                _string(
                    errors,
                    action["name"],
                    f"{location}.action.name",
                )
        else:
            _unexpected(errors, action, {"type"}, f"{location}.action")

        effects = step.get("effects")
        if not isinstance(effects, list) or not effects:
            errors.append(f"{location}.effects: non-empty array is required")
        elif not all(
            isinstance(effect, str) and effect.strip()
            for effect in effects
        ):
            errors.append(f"{location}.effects: strings are required")
        approval = step.get("approval")
        retry_policy = step.get("retry_policy")
        if approval not in APPROVAL_VALUES:
            errors.append(
                f"{location}.approval: expected one of "
                f"{sorted(APPROVAL_VALUES)}"
            )
        if retry_policy not in RETRY_VALUES:
            errors.append(
                f"{location}.retry_policy: expected one of "
                f"{sorted(RETRY_VALUES)}"
            )
        if is_mutating_step(step):
            if approval != "required":
                errors.append(
                    f"{location}.approval: mutation requires approval"
                )
            if retry_policy != "never":
                errors.append(
                    f"{location}.retry_policy: mutation must never auto-retry"
                )
        timeout_ms = step.get("timeout_ms", 30_000)
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or not 1 <= timeout_ms <= 120_000
        ):
            errors.append(f"{location}.timeout_ms: 1..120000 required")
        hold_ms = step.get("hold_ms", 800)
        if (
            not isinstance(hold_ms, int)
            or isinstance(hold_ms, bool)
            or not 0 <= hold_ms <= 30_000
        ):
            errors.append(f"{location}.hold_ms: 0..30000 required")

    if launch_count != 1:
        errors.append("steps: exactly one first launch action is required")
    return errors


def native_scenario_summary(
    scenario: Mapping[str, Any],
) -> Dict[str, Any]:
    steps = scenario.get("steps", [])
    mutations = [
        {
            "id": step.get("id"),
            "title": step.get("title"),
            "effects": step.get("effects", []),
        }
        for step in steps
        if isinstance(step, dict) and is_mutating_step(step)
    ]
    return {
        "title": scenario.get("title"),
        "backend": "native-android",
        "platform": scenario.get("platform"),
        "app_artifact_id": scenario.get("app_artifact_id"),
        "package_id": scenario.get("package_id"),
        "device": scenario.get("device"),
        "output_size": {"width": 1920, "height": 1080},
        "step_count": len(steps) if isinstance(steps, list) else 0,
        "mutation_count": len(mutations),
        "mutations": mutations,
    }
