"""Generic Playwright action executor used by recording workers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

from .errors import ValidationError
from .models import url_origin


def _text_value(target: Mapping[str, Any], key: str = "value") -> Any:
    value = target[key]
    if target.get("regex"):
        return re.compile(value)
    return value


def locator_for(page: Page, target: Mapping[str, Any]) -> Locator:
    kind = target["by"]
    value = target["value"]
    exact = bool(target.get("exact", False))
    if kind == "role":
        name = target.get("name")
        locator = page.get_by_role(
            value,
            name=(
                re.compile(name)
                if name is not None and target.get("regex")
                else name
            ),
            exact=exact,
        )
    elif kind == "text":
        locator = page.get_by_text(_text_value(target), exact=exact)
    elif kind == "label":
        locator = page.get_by_label(_text_value(target), exact=exact)
    elif kind == "placeholder":
        locator = page.get_by_placeholder(_text_value(target), exact=exact)
    elif kind == "test_id":
        locator = page.get_by_test_id(value)
    elif kind == "css":
        locator = page.locator(value)
    else:
        raise ValidationError(f"unsupported selector kind: {kind}")

    if "has_text" in target:
        has_text = target["has_text"]
        if isinstance(has_text, list):
            for item in has_text:
                locator = locator.filter(has_text=item)
        else:
            locator = locator.filter(has_text=has_text)
    if "nth" in target:
        locator = locator.nth(target["nth"])
    return locator


def require_unique(locator: Locator, action_type: str) -> None:
    count = locator.count()
    if count != 1:
        raise RuntimeError(
            f"{action_type} target must resolve to exactly one element; "
            f"found {count}"
        )


def _allowed_navigation(url: str, allowed_origins: list[str]) -> None:
    origin = url_origin(url)
    if origin not in allowed_origins:
        raise RuntimeError(f"navigation origin is not allowed: {origin}")


def execute_action(
    page: Page,
    action: Mapping[str, Any],
    *,
    allowed_origins: list[str],
    artifact_dir: Path,
    plugin_runtime: Optional[Any] = None,
) -> None:
    action_type = action["type"]
    timeout = int(action.get("timeout_ms", 30_000))
    if action_type == "goto":
        url = action["url"]
        _allowed_navigation(url, allowed_origins)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return
    if action_type == "scroll":
        page.evaluate(
            """top => {
                const candidates = [
                    document.scrollingElement,
                    document.querySelector("main"),
                    document.querySelector('[role="main"]'),
                    ...Array.from(document.querySelectorAll("div"))
                        .filter((element) =>
                            element.scrollHeight >
                                element.clientHeight + 100 &&
                            ["auto", "scroll"].includes(
                                getComputedStyle(element).overflowY
                            )
                        )
                        .sort((a, b) =>
                            (b.scrollHeight - b.clientHeight) -
                            (a.scrollHeight - a.clientHeight)
                        )
                ].filter(Boolean);
                const surface = candidates[0];
                if (!surface) return false;
                surface.scrollTo({top, behavior: "smooth"});
                return true;
            }""",
            action["top"],
        )
        return
    if action_type == "pause":
        page.wait_for_timeout(action["milliseconds"])
        return
    if action_type == "screenshot":
        name = action.get("name", "capture")
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
        page.screenshot(
            path=str(artifact_dir / f"{safe_name}.png"),
            full_page=bool(action.get("full_page", False)),
        )
        return
    if action_type == "plugin":
        if plugin_runtime is None or not hasattr(
            plugin_runtime, "execute_operation"
        ):
            raise RuntimeError("plugin runtime is not available")
        plugin_runtime.execute_operation(
            page,
            action["operation"],
            action.get("params", {}),
        )
        return

    target = action["target"]
    locator = locator_for(page, target)
    if action_type == "click":
        locator.click(timeout=timeout)
    elif action_type == "fill":
        locator.fill(action["value"], timeout=timeout)
    elif action_type == "press":
        locator.press(action["value"], timeout=timeout)
    elif action_type == "select_option":
        locator.select_option(action["value"], timeout=timeout)
    elif action_type == "wait_for":
        locator.wait_for(
            state=action.get("state", "visible"),
            timeout=timeout,
        )
        if action.get("strict", True):
            require_unique(locator, action_type)
    elif action_type == "assert":
        state = action.get("state", "visible")
        locator.wait_for(state=state, timeout=timeout)
        if action.get("strict", True):
            require_unique(locator, action_type)
    else:
        raise RuntimeError(f"unsupported action type: {action_type}")


def assert_completion(
    page: Page,
    assertion: Mapping[str, Any],
    *,
    timeout_ms: int = 30_000,
) -> None:
    assertion_type = assertion["type"]
    if assertion_type == "url":
        expected = assertion["value"]
        if assertion.get("regex"):
            page.wait_for_url(re.compile(expected), timeout=timeout_ms)
        else:
            page.wait_for_url(expected, timeout=timeout_ms)
        return
    locator = locator_for(page, assertion["target"])
    if assertion_type == "visible":
        locator.wait_for(state="visible", timeout=timeout_ms)
    elif assertion_type == "hidden":
        locator.wait_for(state="hidden", timeout=timeout_ms)
    elif assertion_type == "count":
        actual = locator.count()
        if actual != assertion["value"]:
            raise RuntimeError(
                f"completion count expected {assertion['value']}, got {actual}"
            )
    elif assertion_type == "text":
        locator.filter(has_text=assertion["value"]).wait_for(
            state="visible",
            timeout=timeout_ms,
        )
    else:
        raise RuntimeError(
            f"unsupported completion assertion: {assertion_type}"
        )
