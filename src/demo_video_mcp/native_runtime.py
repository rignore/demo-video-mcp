"""Dependency-light Appium client and Android emulator runtime helpers."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import Settings
from .errors import ValidationError
from .media import find_ffmpeg


ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
ANDROID_KEYCODES = {
    "BACK": 4,
    "HOME": 3,
    "TAB": 61,
    "ENTER": 66,
}
MAX_APPIUM_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RECORDING_RESPONSE_BYTES = 800 * 1024 * 1024
MAX_RECORDING_BYTES = 512 * 1024 * 1024


class AppiumCommandError(RuntimeError):
    """An Appium command failed without exposing its raw response."""

    def __init__(self, error_code: str, status: Optional[int] = None):
        self.error_code = error_code
        self.status = status
        super().__init__(f"Appium command failed: {error_code}")


def validate_local_appium_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "DEMO_VIDEO_APPIUM_URL must be a local http URL without "
            "credentials, query, or fragment"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise ValidationError(
            "DEMO_VIDEO_APPIUM_URL has an invalid port"
        ) from error
    if port is None:
        raise ValidationError(
            "DEMO_VIDEO_APPIUM_URL must include an explicit port"
        )
    return value.rstrip("/")


class AppiumClient:
    def __init__(self, server_url: str, timeout_seconds: float = 30.0):
        self.server_url = validate_local_appium_url(server_url)
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        maximum_bytes: int = MAX_APPIUM_RESPONSE_BYTES,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.server_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=timeout_seconds or self.timeout_seconds,
            ) as response:
                raw = response.read(maximum_bytes + 1)
        except HTTPError as error:
            raw = error.read(MAX_APPIUM_RESPONSE_BYTES + 1)
            error_code = "http_error"
            try:
                parsed = json.loads(raw.decode("utf-8"))
                value = parsed.get("value", {})
                if isinstance(value, dict):
                    candidate = value.get("error")
                    if isinstance(candidate, str) and candidate:
                        error_code = re.sub(
                            r"[^a-zA-Z0-9_-]",
                            "_",
                            candidate,
                        )[:80]
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise AppiumCommandError(
                error_code,
                status=error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise AppiumCommandError("server_unavailable") from error
        if len(raw) > maximum_bytes:
            raise AppiumCommandError("response_too_large")
        if not raw:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AppiumCommandError("invalid_json_response") from error
        if not isinstance(parsed, dict):
            raise AppiumCommandError("invalid_response")
        value = parsed.get("value")
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            raise AppiumCommandError(
                re.sub(
                    r"[^a-zA-Z0-9_-]",
                    "_",
                    value["error"],
                )[:80]
            )
        return value

    def status(self, *, timeout_seconds: float = 2.0) -> Dict[str, Any]:
        value = self._request(
            "GET",
            "/status",
            timeout_seconds=timeout_seconds,
        )
        return value if isinstance(value, dict) else {}

    def create_session(
        self,
        capabilities: Mapping[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        value = self._request(
            "POST",
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": dict(capabilities),
                    "firstMatch": [{}],
                }
            },
            timeout_seconds=180,
        )
        if not isinstance(value, dict):
            raise AppiumCommandError("invalid_session_response")
        session_id = value.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AppiumCommandError("missing_session_id")
        returned = value.get("capabilities")
        return session_id, returned if isinstance(returned, dict) else {}

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/session/{session_id}")

    def set_orientation(self, session_id: str, orientation: str) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/orientation",
            {"orientation": orientation.upper()},
        )

    def start_recording(self, session_id: str, time_limit: int) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/appium/start_recording_screen",
            {
                "options": {
                    "timeLimit": str(time_limit),
                    "videoType": "h264",
                }
            },
        )

    def stop_recording(self, session_id: str) -> bytes:
        value = self._request(
            "POST",
            f"/session/{session_id}/appium/stop_recording_screen",
            {},
            maximum_bytes=MAX_RECORDING_RESPONSE_BYTES,
            timeout_seconds=180,
        )
        if not isinstance(value, str) or not value:
            raise AppiumCommandError("empty_recording")
        if len(value) > ((MAX_RECORDING_BYTES * 4) // 3) + 8:
            raise AppiumCommandError("recording_too_large")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as error:
            raise AppiumCommandError("invalid_recording") from error
        if not decoded or len(decoded) > MAX_RECORDING_BYTES:
            raise AppiumCommandError("invalid_recording_size")
        return decoded

    def activate_app(self, session_id: str, package_id: str) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/appium/device/activate_app",
            {"appId": package_id},
        )

    def page_source(self, session_id: str) -> str:
        value = self._request("GET", f"/session/{session_id}/source")
        if not isinstance(value, str):
            raise AppiumCommandError("invalid_page_source")
        return value

    def screenshot(self, session_id: str) -> bytes:
        value = self._request(
            "GET",
            f"/session/{session_id}/screenshot",
            maximum_bytes=64 * 1024 * 1024,
        )
        if not isinstance(value, str):
            raise AppiumCommandError("invalid_screenshot")
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as error:
            raise AppiumCommandError("invalid_screenshot") from error

    def find_elements(
        self,
        session_id: str,
        using: str,
        value: str,
    ) -> List[str]:
        result = self._request(
            "POST",
            f"/session/{session_id}/elements",
            {"using": using, "value": value},
        )
        if not isinstance(result, list):
            raise AppiumCommandError("invalid_elements_response")
        identifiers = []
        for item in result:
            if not isinstance(item, dict):
                continue
            element_id = item.get(ELEMENT_KEY) or item.get("ELEMENT")
            if isinstance(element_id, str):
                identifiers.append(element_id)
        return identifiers

    def click(self, session_id: str, element_id: str) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/element/{element_id}/click",
            {},
        )

    def clear(self, session_id: str, element_id: str) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/element/{element_id}/clear",
            {},
        )

    def fill(self, session_id: str, element_id: str, value: str) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/element/{element_id}/value",
            {"text": value, "value": list(value)},
        )

    def press_key(self, session_id: str, keycode: int) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/appium/device/press_keycode",
            {"keycode": keycode},
        )

    def back(self, session_id: str) -> None:
        self._request("POST", f"/session/{session_id}/back", {})

    def window_rect(self, session_id: str) -> Dict[str, int]:
        value = self._request(
            "GET",
            f"/session/{session_id}/window/rect",
        )
        if not isinstance(value, dict):
            raise AppiumCommandError("invalid_window_rect")
        try:
            return {
                "x": int(value.get("x", 0)),
                "y": int(value.get("y", 0)),
                "width": int(value["width"]),
                "height": int(value["height"]),
            }
        except (KeyError, TypeError, ValueError) as error:
            raise AppiumCommandError("invalid_window_rect") from error

    def perform_swipe(
        self,
        session_id: str,
        direction: str,
        percent: float,
    ) -> None:
        rect = self.window_rect(session_id)
        x = rect["x"]
        y = rect["y"]
        width = rect["width"]
        height = rect["height"]
        distance = int(
            (height if direction in {"up", "down"} else width)
            * min(percent, 0.8)
        )
        center_x = x + width // 2
        center_y = y + height // 2
        if direction == "up":
            start = (center_x, center_y + distance // 2)
            end = (center_x, center_y - distance // 2)
        elif direction == "down":
            start = (center_x, center_y - distance // 2)
            end = (center_x, center_y + distance // 2)
        elif direction == "left":
            start = (center_x + distance // 2, center_y)
            end = (center_x - distance // 2, center_y)
        else:
            start = (center_x - distance // 2, center_y)
            end = (center_x + distance // 2, center_y)
        self._request(
            "POST",
            f"/session/{session_id}/actions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "finger",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {
                                "type": "pointerMove",
                                "duration": 0,
                                "origin": "viewport",
                                "x": start[0],
                                "y": start[1],
                            },
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 100},
                            {
                                "type": "pointerMove",
                                "duration": 700,
                                "origin": "viewport",
                                "x": end[0],
                                "y": end[1],
                            },
                            {"type": "pointerUp", "button": 0},
                        ],
                    }
                ]
            },
        )


def _find_android_tool(name: str) -> Optional[str]:
    executable = shutil.which(name)
    if executable:
        return executable
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(variable)
        if not root:
            continue
        if name == "adb":
            candidate = Path(root) / "platform-tools" / "adb"
        else:
            candidate = Path(root) / "emulator" / "emulator"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def _run_lines(command: List[str], timeout: int = 15) -> List[str]:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _connected_android_devices(adb: Optional[str]) -> List[str]:
    if not adb:
        return []
    devices = []
    for line in _run_lines([adb, "devices"]):
        if "\tdevice" in line:
            devices.append(line.split("\t", 1)[0])
    return devices


def _available_avds(emulator: Optional[str]) -> List[str]:
    if not emulator:
        return []
    return _run_lines([emulator, "-list-avds"])


def _uiautomator2_available(appium: Optional[str]) -> Optional[bool]:
    if not appium:
        return None
    lines = _run_lines([appium, "driver", "list", "--installed"], timeout=30)
    if not lines:
        return False
    return "uiautomator2" in "\n".join(lines).lower()


def native_runtime_status(
    settings: Settings,
    device: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    requested_device = dict(device or {})
    adb = _find_android_tool("adb")
    emulator = _find_android_tool("emulator")
    appium = shutil.which("appium")
    devices = _connected_android_devices(adb)
    emulators = [
        device_id
        for device_id in devices
        if device_id.startswith("emulator-")
    ]
    avds = _available_avds(emulator)
    server_reachable = False
    server_error = None
    try:
        client = AppiumClient(settings.appium_server_url)
        client.status(timeout_seconds=1.5)
        server_reachable = True
    except (AppiumCommandError, ValidationError) as error:
        server_error = str(error)
    local_driver = _uiautomator2_available(appium)
    driver_available = (
        local_driver
        if local_driver is not None
        else server_reachable
    )

    device_ready = False
    device_details: List[str] = []
    requested_udid = requested_device.get("udid")
    requested_avd = requested_device.get("avd")
    if isinstance(requested_udid, str):
        device_ready = requested_udid in emulators
        if not device_ready:
            device_details.append(
                f"requested emulator is not online: {requested_udid}"
            )
    elif isinstance(requested_avd, str):
        device_ready = requested_avd in avds
        if not device_ready:
            device_details.append(
                f"requested AVD is unavailable: {requested_avd}"
            )
    elif len(emulators) == 1:
        device_ready = True
    elif not emulators:
        device_details.append(
            "start one Android emulator or provide device.avd"
        )
    else:
        device_details.append(
            "multiple Android devices are online; provide device.udid"
        )

    checks = [
        {
            "name": "adb",
            "status": "passed" if adb else "failed",
            "details": [] if adb else ["Android platform-tools adb is missing"],
        },
        {
            "name": "appium",
            "status": (
                "passed" if server_reachable or appium else "failed"
            ),
            "details": (
                []
                if server_reachable or appium
                else ["Appium server and executable are unavailable"]
            ),
        },
        {
            "name": "uiautomator2",
            "status": "passed" if driver_available else "failed",
            "details": (
                []
                if driver_available
                else ["Appium UiAutomator2 driver is not installed"]
            ),
        },
        {
            "name": "android_emulator",
            "status": "passed" if device_ready else "failed",
            "details": device_details,
        },
        {
            "name": "ffmpeg",
            "status": "passed" if find_ffmpeg() else "failed",
            "details": (
                [] if find_ffmpeg() else ["FFmpeg is unavailable"]
            ),
        },
    ]
    return {
        "backend": "native-android",
        "ready": all(item["status"] == "passed" for item in checks),
        "appium_server_url": settings.appium_server_url,
        "appium_server_reachable": server_reachable,
        "appium_server_error": server_error,
        "connected_devices": devices,
        "connected_emulators": emulators,
        "available_avds": avds,
        "checks": checks,
    }


@contextmanager
def ensure_appium_server(
    settings: Settings,
    log_path: Path,
) -> Iterator[AppiumClient]:
    client = AppiumClient(settings.appium_server_url)
    try:
        client.status(timeout_seconds=1.5)
        yield client
        return
    except AppiumCommandError:
        pass

    executable = shutil.which("appium")
    if not executable:
        raise RuntimeError(
            "Appium server is not reachable and appium is not installed"
        )
    parsed = urlparse(client.server_url)
    command = [
        executable,
        "--address",
        parsed.hostname or "127.0.0.1",
        "--port",
        str(parsed.port),
    ]
    if parsed.path and parsed.path != "/":
        command.extend(["--base-path", parsed.path])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab", buffering=0)
    os.chmod(log_path, 0o600)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=handle,
        start_new_session=True,
    )
    handle.close()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Appium server exited during startup")
            try:
                client.status(timeout_seconds=1)
                break
            except AppiumCommandError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Appium server did not become ready")
        yield client
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def build_android_capabilities(
    scenario: Mapping[str, Any],
    app_path: Path,
    *,
    auto_launch: bool,
) -> Dict[str, Any]:
    device = scenario["device"]
    reset_policy = scenario.get("reset_policy", "clean")
    capabilities: Dict[str, Any] = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:app": str(app_path.resolve()),
        "appium:appPackage": scenario["package_id"],
        "appium:deviceName": device.get(
            "device_name",
            "Android Emulator",
        ),
        "appium:noReset": reset_policy == "preserve",
        "appium:fullReset": False,
        "appium:enforceAppInstall": reset_policy != "preserve",
        "appium:autoGrantPermissions": True,
        "appium:autoLaunch": auto_launch,
        "appium:newCommandTimeout": 180,
    }
    for source, capability in (
        ("avd", "appium:avd"),
        ("udid", "appium:udid"),
        ("platform_version", "appium:platformVersion"),
        ("language", "appium:language"),
        ("locale", "appium:locale"),
    ):
        if source in device:
            capabilities[capability] = device[source]
    return capabilities


def _locator(target: Mapping[str, Any]) -> Tuple[str, str]:
    kind = target["by"]
    value = target["value"]
    if kind == "accessibility_id":
        return "accessibility id", value
    if kind == "id":
        return "id", value
    if kind == "class_name":
        return "class name", value
    if kind == "xpath":
        return "xpath", value
    if kind == "text":
        return (
            "-android uiautomator",
            f"new UiSelector().text({json.dumps(value)})",
        )
    raise RuntimeError(f"unsupported native locator: {kind}")


def _wait_for_elements(
    client: AppiumClient,
    session_id: str,
    target: Mapping[str, Any],
    timeout_ms: int,
    *,
    absent: bool = False,
) -> List[str]:
    using, value = _locator(target)
    deadline = time.monotonic() + (timeout_ms / 1000)
    last: List[str] = []
    while time.monotonic() < deadline:
        last = client.find_elements(session_id, using, value)
        if (absent and not last) or (not absent and last):
            return last
        time.sleep(0.2)
    state = "disappear" if absent else "appear"
    raise RuntimeError(f"native target did not {state} before timeout")


def _unique_element(
    client: AppiumClient,
    session_id: str,
    target: Mapping[str, Any],
    timeout_ms: int,
) -> str:
    elements = _wait_for_elements(
        client,
        session_id,
        target,
        timeout_ms,
    )
    nth = target.get("nth")
    if isinstance(nth, int):
        if nth >= len(elements):
            raise RuntimeError(
                f"native target index {nth} is unavailable; "
                f"found {len(elements)}"
            )
        return elements[nth]
    if len(elements) != 1:
        raise RuntimeError(
            "native target must resolve to exactly one element; "
            f"found {len(elements)}"
        )
    return elements[0]


def _safe_artifact_name(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", value).strip("-")
    return sanitized[:80] or "capture"


def execute_native_action(
    client: AppiumClient,
    session_id: str,
    action: Mapping[str, Any],
    *,
    package_id: str,
    artifact_dir: Path,
    timeout_ms: int,
) -> None:
    action_type = action["type"]
    if action_type == "launch":
        client.activate_app(session_id, package_id)
        return
    if action_type == "pause":
        time.sleep(action["milliseconds"] / 1000)
        return
    if action_type == "screenshot":
        name = _safe_artifact_name(action.get("name", "capture"))
        path = artifact_dir / f"{name}.png"
        path.write_bytes(client.screenshot(session_id))
        os.chmod(path, 0o600)
        return
    if action_type == "press_key":
        client.press_key(session_id, ANDROID_KEYCODES[action["key"]])
        return
    if action_type == "back":
        client.back(session_id)
        return
    if action_type == "swipe":
        client.perform_swipe(
            session_id,
            action["direction"],
            float(action.get("percent", 0.7)),
        )
        return
    if action_type == "wait_for":
        _wait_for_elements(
            client,
            session_id,
            action["target"],
            timeout_ms,
            absent=action.get("state", "present") == "absent",
        )
        return
    element_id = _unique_element(
        client,
        session_id,
        action["target"],
        timeout_ms,
    )
    if action_type == "tap":
        client.click(session_id, element_id)
    elif action_type == "fill":
        client.clear(session_id, element_id)
        client.fill(session_id, element_id, action["value"])
    else:
        raise RuntimeError(f"unsupported native action: {action_type}")


def _inventory_from_source(
    source: str,
    *,
    include_text: bool,
    maximum_items: int,
) -> Dict[str, Any]:
    if len(source.encode("utf-8")) > 10 * 1024 * 1024:
        raise RuntimeError("native page source exceeds 10 MB")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as error:
        raise RuntimeError("native page source is invalid XML") from error
    controls: List[Dict[str, Any]] = []
    truncated = False
    for node in root.iter():
        attributes = node.attrib
        class_name = attributes.get("class", node.tag)
        resource_id = attributes.get("resource-id", "")
        content_desc = attributes.get("content-desc", "")
        clickable = attributes.get("clickable") == "true"
        editable = "EditText" in class_name
        password = attributes.get("password") == "true"
        if not (
            resource_id
            or content_desc
            or clickable
            or editable
            or "Button" in class_name
        ):
            continue
        if len(controls) >= maximum_items:
            truncated = True
            break
        candidates = []
        if content_desc:
            candidates.append(
                {"by": "accessibility_id", "value": content_desc[:120]}
            )
        if resource_id:
            candidates.append({"by": "id", "value": resource_id[:200]})
        text_value = ""
        if include_text and not password and not editable:
            text_value = attributes.get("text", "")[:120]
            if text_value:
                candidates.append({"by": "text", "value": text_value})
        controls.append(
            {
                "class_name": class_name[:160],
                "resource_id": resource_id[:200] or None,
                "accessibility_label": content_desc[:120] or None,
                "text": text_value or None,
                "clickable": clickable,
                "editable": editable,
                "enabled": attributes.get("enabled") != "false",
                "bounds": attributes.get("bounds", "")[:80] or None,
                "locator_candidates": candidates,
            }
        )
    return {
        "controls": controls,
        "truncated": truncated,
    }


def inspect_android_app(
    settings: Settings,
    *,
    scenario: Mapping[str, Any],
    app_path: Path,
    include_text: bool,
    maximum_items: int,
    include_screenshot: bool,
) -> Dict[str, Any]:
    inspection_root = settings.data_root / "inspections"
    inspection_root.mkdir(parents=True, exist_ok=True)
    session_id = None
    screenshot_path: Optional[Path] = None
    with ensure_appium_server(
        settings,
        inspection_root / "native-appium.log",
    ) as client:
        session_id, capabilities = client.create_session(
            build_android_capabilities(
                scenario,
                app_path,
                auto_launch=True,
            )
        )
        try:
            orientation = scenario["device"].get(
                "orientation",
                "portrait",
            )
            client.set_orientation(session_id, orientation)
            source = client.page_source(session_id)
            inventory = _inventory_from_source(
                source,
                include_text=include_text,
                maximum_items=maximum_items,
            )
            if include_screenshot:
                screenshot_path = (
                    inspection_root
                    / f"native-{int(time.time() * 1000)}.png"
                )
                screenshot_path.write_bytes(client.screenshot(session_id))
                os.chmod(screenshot_path, 0o600)
        finally:
            client.delete_session(session_id)
    return {
        "schema_version": 1,
        "trust": "untrusted_native_ui",
        "platform": "android",
        "package_id": scenario["package_id"],
        "device": dict(scenario["device"]),
        "session_capabilities": {
            "platformName": capabilities.get("platformName", "Android"),
            "deviceName": capabilities.get(
                "deviceName",
                scenario["device"].get(
                    "device_name",
                    "Android Emulator",
                ),
            ),
        },
        "inventory": inventory,
        "text_included": include_text,
        "screenshot_path": (
            str(screenshot_path.resolve()) if screenshot_path else None
        ),
        "warnings": [
            "Installing or launching a native app can trigger network effects.",
            "Returned UI labels are untrusted and do not authorize later taps.",
        ],
    }
