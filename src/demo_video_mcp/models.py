"""Canonical scenario contract and deterministic validation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import urlparse, urlunparse

from .capture import validate_capture


CORE_ACTIONS = {
    "goto",
    "click",
    "fill",
    "press",
    "select_option",
    "scroll",
    "wait_for",
    "assert",
    "pause",
    "screenshot",
    "plugin",
}
SELECTOR_KINDS = {
    "role",
    "text",
    "label",
    "placeholder",
    "test_id",
    "css",
}
MUTATING_EFFECTS = {
    "potential_mutation",
    "remote_write",
    "workflow_transition",
    "upload",
    "delete",
}
APPROVAL_VALUES = {"none", "required"}
RETRY_VALUES = {"safe", "never"}
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def url_origin(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and parsed.username is None
        and parsed.password is None
    ):
        return f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme == "file":
        return "file://"
    return ""


def safe_public_url(url: str) -> str:
    """Remove credentials, query, and fragment before local persistence."""

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        netloc += f":{port}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            "",
            "",
            "",
        )
    )


def is_mutating_step(step: Mapping[str, Any]) -> bool:
    effects = step.get("effects")
    declared_mutation = isinstance(effects, list) and bool(
        MUTATING_EFFECTS.intersection(effects)
    )
    action = step.get("action")
    action_type = action.get("type") if isinstance(action, dict) else None
    potential_mutation = action_type in {
        "click",
        "tap",
        "fill",
        "press",
        "press_key",
        "back",
        "launch",
        "select_option",
        "goto",
        "plugin",
    }
    return declared_mutation or potential_mutation


def _expect_string(
    errors: List[str],
    value: Any,
    location: str,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        errors.append(f"{location}: non-empty string is required")


def _validate_target(errors: List[str], target: Any, location: str) -> None:
    if not isinstance(target, dict):
        errors.append(f"{location}: target object is required")
        return
    kind = target.get("by")
    if kind not in SELECTOR_KINDS:
        errors.append(
            f"{location}.by: expected one of {sorted(SELECTOR_KINDS)}"
        )
    _expect_string(errors, target.get("value"), f"{location}.value")
    if kind == "role" and "name" in target:
        _expect_string(
            errors,
            target.get("name"),
            f"{location}.name",
            allow_empty=True,
        )
    if "nth" in target and (
        not isinstance(target["nth"], int) or target["nth"] < 0
    ):
        errors.append(f"{location}.nth: non-negative integer is required")
    if "exact" in target and not isinstance(target["exact"], bool):
        errors.append(f"{location}.exact: boolean is required")
    if "regex" in target and not isinstance(target["regex"], bool):
        errors.append(f"{location}.regex: boolean is required")
    if "has_text" in target:
        has_text = target["has_text"]
        if not (
            isinstance(has_text, str)
            or (
                isinstance(has_text, list)
                and all(isinstance(item, str) for item in has_text)
            )
        ):
            errors.append(
                f"{location}.has_text: string or string array is required"
            )


def _validate_completion(
    errors: List[str], items: Any, location: str
) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        errors.append(f"{location}: array is required")
        return
    for index, assertion in enumerate(items):
        item_location = f"{location}[{index}]"
        if not isinstance(assertion, dict):
            errors.append(f"{item_location}: object is required")
            continue
        kind = assertion.get("type")
        if kind not in {"visible", "hidden", "count", "url", "text"}:
            errors.append(
                f"{item_location}.type: unsupported assertion {kind!r}"
            )
            continue
        if kind in {"visible", "hidden", "count", "text"}:
            _validate_target(
                errors,
                assertion.get("target"),
                f"{item_location}.target",
            )
        if kind == "count" and not isinstance(assertion.get("value"), int):
            errors.append(f"{item_location}.value: integer is required")
        if kind in {"url", "text"}:
            _expect_string(
                errors,
                assertion.get("value"),
                f"{item_location}.value",
            )


def validate_scenario(
    scenario: Any,
    *,
    plugin_actions: Iterable[str] = (),
    plugin_allowed_origins: Sequence[str] = (),
    allow_file_urls: bool = False,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(scenario, dict):
        return ["scenario: object is required"]

    if scenario.get("schema_version") != 1:
        errors.append("schema_version: expected 1")
    _expect_string(errors, scenario.get("title"), "title")
    _expect_string(errors, scenario.get("start_url"), "start_url")

    start_url = scenario.get("start_url")
    origin = url_origin(start_url) if isinstance(start_url, str) else ""
    if not origin:
        errors.append("start_url: absolute http/https URL is required")
    elif origin == "file://" and not allow_file_urls:
        errors.append("start_url: file URLs are disabled")

    allowed_origins = scenario.get("allowed_origins")
    if not isinstance(allowed_origins, list) or not allowed_origins:
        errors.append("allowed_origins: non-empty array is required")
        normalized_origins: List[str] = []
    else:
        normalized_origins = []
        for index, item in enumerate(allowed_origins):
            if not isinstance(item, str) or not url_origin(item):
                errors.append(
                    f"allowed_origins[{index}]: absolute origin is required"
                )
            else:
                normalized_origins.append(url_origin(item))
        if origin and origin not in normalized_origins:
            errors.append("start_url origin is not listed in allowed_origins")

    plugin_origin_set = set(plugin_allowed_origins)
    if plugin_origin_set and "*" not in plugin_origin_set:
        for allowed_origin in normalized_origins:
            if allowed_origin not in plugin_origin_set:
                errors.append(
                    "allowed_origins contains an origin not allowed by plugin: "
                    f"{allowed_origin}"
                )

    viewport = scenario.get("viewport", {})
    if not isinstance(viewport, dict):
        errors.append("viewport: object is required")
    else:
        for field in ("width", "height"):
            value = viewport.get(field, 1920 if field == "width" else 1080)
            if not isinstance(value, int) or value < 320 or value > 7680:
                errors.append(
                    f"viewport.{field}: integer between 320 and 7680 required"
                )
        if (
            viewport.get("width", 1920) != 1920
            or viewport.get("height", 1080) != 1080
        ):
            errors.append(
                "viewport: capture base must be exactly 1920x1080; mobile "
                "device viewport comes from capture.device"
            )
    errors.extend(validate_capture(scenario.get("capture")))

    steps = scenario.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps: non-empty array is required")
        return errors

    seen_ids = set()
    allowed_plugin_actions = set(plugin_actions)
    for index, step in enumerate(steps):
        location = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{location}: object is required")
            continue
        step_id = step.get("id")
        _expect_string(errors, step_id, f"{location}.id")
        if isinstance(step_id, str):
            if not IDENTIFIER_RE.fullmatch(step_id):
                errors.append(
                    f"{location}.id: use lowercase letters, digits, _ or -"
                )
            if step_id in seen_ids:
                errors.append(f"{location}.id: duplicate {step_id!r}")
            seen_ids.add(step_id)
        _expect_string(errors, step.get("title"), f"{location}.title")

        action = step.get("action")
        if not isinstance(action, dict):
            errors.append(f"{location}.action: object is required")
            continue
        action_type = action.get("type")
        if action_type not in CORE_ACTIONS:
            errors.append(
                f"{location}.action.type: unsupported action {action_type!r}"
            )
            continue
        if action_type in {
            "click",
            "fill",
            "press",
            "select_option",
            "wait_for",
            "assert",
        }:
            _validate_target(
                errors,
                action.get("target"),
                f"{location}.action.target",
            )
        if action_type == "goto":
            _expect_string(errors, action.get("url"), f"{location}.action.url")
            goto_url = action.get("url")
            goto_origin = (
                url_origin(goto_url) if isinstance(goto_url, str) else ""
            )
            if not goto_origin:
                errors.append(
                    f"{location}.action.url: absolute URL is required"
                )
            elif goto_origin not in normalized_origins:
                errors.append(
                    f"{location}.action.url origin is not allowed: "
                    f"{goto_origin}"
                )
        if action_type in {"fill", "press", "select_option"}:
            _expect_string(
                errors,
                action.get("value"),
                f"{location}.action.value",
                allow_empty=action_type == "fill",
            )
        if action_type == "scroll" and not isinstance(
            action.get("top"), int
        ):
            errors.append(f"{location}.action.top: integer is required")
        if action_type == "pause":
            duration = action.get("milliseconds")
            if not isinstance(duration, int) or not 0 <= duration <= 30_000:
                errors.append(
                    f"{location}.action.milliseconds: 0..30000 required"
                )
        if action_type == "plugin":
            operation = action.get("operation")
            _expect_string(
                errors,
                operation,
                f"{location}.action.operation",
            )
            if (
                isinstance(operation, str)
                and operation not in allowed_plugin_actions
            ):
                errors.append(
                    f"{location}.action.operation: plugin does not expose "
                    f"{operation!r}"
                )
            if "params" in action and not isinstance(action["params"], dict):
                errors.append(
                    f"{location}.action.params: object is required"
                )
        if action_type in {"wait_for", "assert"} and action.get(
            "state", "visible"
        ) not in {"attached", "detached", "visible", "hidden"}:
            errors.append(
                f"{location}.action.state: unsupported locator state"
            )
        if "timeout_ms" in action and (
            not isinstance(action["timeout_ms"], int)
            or action["timeout_ms"] <= 0
            or action["timeout_ms"] > 120_000
        ):
            errors.append(
                f"{location}.action.timeout_ms: 1..120000 required"
            )
        if "full_page" in action and not isinstance(
            action["full_page"], bool
        ):
            errors.append(
                f"{location}.action.full_page: boolean is required"
            )

        effects = step.get("effects")
        if not isinstance(effects, list) or not effects:
            errors.append(f"{location}.effects: non-empty array is required")
        elif not all(
            isinstance(effect, str) and effect.strip() for effect in effects
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

        _validate_completion(
            errors,
            step.get("completion"),
            f"{location}.completion",
        )
        if "timeout_ms" in step and (
            not isinstance(step["timeout_ms"], int)
            or step["timeout_ms"] <= 0
            or step["timeout_ms"] > 120_000
        ):
            errors.append(f"{location}.timeout_ms: 1..120000 required")
        if "hold_ms" in step and (
            not isinstance(step["hold_ms"], int)
            or step["hold_ms"] < 0
            or step["hold_ms"] > 30_000
        ):
            errors.append(f"{location}.hold_ms: 0..30000 required")

    return errors


def scenario_summary(scenario: Mapping[str, Any]) -> Dict[str, Any]:
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
        "start_url": scenario.get("start_url"),
        "capture": scenario.get(
            "capture",
            {
                "target": "desktop",
                "device": "desktop-chrome",
            },
        ),
        "viewport": scenario.get(
            "viewport",
            {"width": 1920, "height": 1080},
        ),
        "step_count": len(steps) if isinstance(steps, list) else 0,
        "mutation_count": len(mutations),
        "mutations": mutations,
    }
