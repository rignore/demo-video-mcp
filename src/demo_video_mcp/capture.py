"""Shared desktop and mobile-web capture configuration."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple


CAPTURE_TARGETS = {"desktop", "mobile", "tablet"}
CAPTURE_DEVICES = {
    "desktop-chrome": {
        "target": "desktop",
        "playwright_name": None,
    },
    "iphone-13": {
        "target": "mobile",
        "playwright_name": "iPhone 13",
    },
    "pixel-7": {
        "target": "mobile",
        "playwright_name": "Pixel 7",
    },
    "ipad-mini": {
        "target": "tablet",
        "playwright_name": "iPad Mini",
    },
}
CAPTURE_ORIENTATIONS = {"portrait", "landscape"}
COLOR_SCHEMES = {"light", "dark", "no-preference"}
STANDARD_OUTPUT_SIZE = {"width": 1920, "height": 1080}
DEFAULT_DEVICE_BY_TARGET = {
    "desktop": "desktop-chrome",
    "mobile": "pixel-7",
    "tablet": "ipad-mini",
}


def validate_capture(
    capture: Any,
    *,
    location: str = "capture",
) -> List[str]:
    errors: List[str] = []
    if capture is None:
        return errors
    if not isinstance(capture, dict):
        return [f"{location}: object is required"]

    allowed = {
        "target",
        "device",
        "orientation",
        "locale",
        "timezone_id",
        "color_scheme",
    }
    unexpected = sorted(set(capture) - allowed)
    if unexpected:
        errors.append(
            f"{location}: unsupported properties: {', '.join(unexpected)}"
        )

    target = capture.get("target")
    device = capture.get("device")
    if target is not None and target not in CAPTURE_TARGETS:
        errors.append(
            f"{location}.target: expected one of {sorted(CAPTURE_TARGETS)}"
        )
    if device is not None and device not in CAPTURE_DEVICES:
        errors.append(
            f"{location}.device: expected one of "
            f"{sorted(CAPTURE_DEVICES)}"
        )
    if device in CAPTURE_DEVICES and target in CAPTURE_TARGETS:
        device_target = CAPTURE_DEVICES[device]["target"]
        if target != device_target:
            errors.append(
                f"{location}.device: {device!r} is a {device_target} device, "
                f"not {target}"
            )

    orientation = capture.get("orientation")
    if orientation is not None and orientation not in CAPTURE_ORIENTATIONS:
        errors.append(
            f"{location}.orientation: expected one of "
            f"{sorted(CAPTURE_ORIENTATIONS)}"
        )
    resolved_target = target
    if resolved_target is None and device in CAPTURE_DEVICES:
        resolved_target = CAPTURE_DEVICES[device]["target"]
    if resolved_target in {None, "desktop"} and orientation == "portrait":
        errors.append(
            f"{location}.orientation: desktop capture must be landscape"
        )
    for field in ("locale", "timezone_id"):
        if field in capture and (
            not isinstance(capture[field], str) or not capture[field].strip()
        ):
            errors.append(f"{location}.{field}: non-empty string is required")
    if (
        "color_scheme" in capture
        and capture["color_scheme"] not in COLOR_SCHEMES
    ):
        errors.append(
            f"{location}.color_scheme: expected one of "
            f"{sorted(COLOR_SCHEMES)}"
        )

    return errors


def normalize_capture(capture: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    errors = validate_capture(capture)
    if errors:
        raise ValueError("\n".join(errors))
    raw = dict(capture or {})
    device = raw.get("device")
    target = raw.get("target")
    if device and not target:
        target = CAPTURE_DEVICES[device]["target"]
    target = target or "desktop"
    device = device or DEFAULT_DEVICE_BY_TARGET[target]
    orientation = raw.get("orientation") or (
        "landscape" if target == "desktop" else "portrait"
    )
    normalized = {
        "target": target,
        "device": device,
        "orientation": orientation,
    }
    for field in ("locale", "timezone_id", "color_scheme"):
        if field in raw:
            normalized[field] = raw[field]
    return normalized


def resolve_capture(
    playwright: Any,
    *,
    capture: Optional[Mapping[str, Any]] = None,
    legacy_viewport: Optional[Mapping[str, int]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return browser.new_context kwargs and public resolved metadata."""

    normalized = normalize_capture(capture)
    device_config = CAPTURE_DEVICES[normalized["device"]]
    playwright_name = device_config["playwright_name"]
    context: Dict[str, Any]
    if playwright_name is None:
        if legacy_viewport and dict(legacy_viewport) != STANDARD_OUTPUT_SIZE:
            raise ValueError(
                "desktop viewport must be exactly 1920x1080"
            )
        viewport = dict(STANDARD_OUTPUT_SIZE)
        context = {
            "viewport": viewport,
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
        }
    else:
        descriptor_name = playwright_name
        if normalized["orientation"] == "landscape":
            descriptor_name += " landscape"
        if descriptor_name not in playwright.devices:
            raise RuntimeError(
                f"Playwright device descriptor is unavailable: "
                f"{descriptor_name}"
            )
        context = dict(playwright.devices[descriptor_name])
        context.pop("default_browser_type", None)
        viewport = dict(context["viewport"])

    for field in ("locale", "timezone_id", "color_scheme"):
        if field in normalized:
            context[field] = normalized[field]
    record_size = dict(viewport)
    metadata = {
        **normalized,
        "browser_engine": "chromium",
        "viewport": dict(viewport),
        "record_size": record_size,
        "output_size": dict(STANDARD_OUTPUT_SIZE),
        "device_scale_factor": context.get("device_scale_factor", 1),
        "is_mobile": bool(context.get("is_mobile", False)),
        "has_touch": bool(context.get("has_touch", False)),
    }
    return context, metadata
