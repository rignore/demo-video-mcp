"""Read-only, single-page inventory for scenario planning."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from .capture import resolve_capture
from .errors import ValidationError
from .models import safe_public_url, url_origin


SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}


def _visible_items(
    page: Any,
    selector: str,
    *,
    kind: str,
    maximum: int,
) -> List[Dict[str, Any]]:
    raw = page.locator(selector).evaluate_all(
        """(elements, maximum) => elements
            .filter((element) => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== "hidden" &&
                    style.display !== "none" &&
                    rect.width > 0 && rect.height > 0;
            })
            .slice(0, maximum)
            .map((element) => ({
                text: (
                    element.getAttribute("aria-label") ||
                    element.innerText ||
                    element.getAttribute("title") ||
                    ""
                ).trim().replace(/\\s+/g, " ").slice(0, 300),
                href: element.href || null
            }))""",
        maximum,
    )
    items: List[Dict[str, Any]] = []
    seen = set()
    for item in raw:
        text = item.get("text", "")
        if not text or text in seen:
            continue
        seen.add(text)
        record: Dict[str, Any] = {
            "text": text,
            "locator_hint": {
                "by": "role",
                "value": kind,
                "name": text,
                "exact": True,
            },
        }
        href = item.get("href")
        if (
            kind == "link"
            and isinstance(href, str)
            and url_origin(href)
        ):
            record["href"] = safe_public_url(href)
        items.append(record)
    return items


def inspect_page(
    *,
    data_root: Path,
    start_url: str,
    allowed_origins: List[str],
    storage_state_path: Optional[Path],
    capture: Optional[Mapping[str, Any]],
    maximum_items: int,
    wait_ms: int,
    include_screenshot: bool,
) -> Dict[str, Any]:
    from playwright.sync_api import sync_playwright

    inspection_id = uuid.uuid4().hex
    screenshot_path = None
    if include_screenshot:
        artifact_dir = data_root / "inspections" / inspection_id
        artifact_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        screenshot_path = artifact_dir / "page.png"
    initial_origin = url_origin(start_url)
    allowed = set(allowed_origins)
    if "*" in allowed:
        allowed = {initial_origin}

    storage_state = None
    if storage_state_path:
        if storage_state_path.is_symlink():
            raise ValidationError("profile storage state cannot be a symlink")
        state_stat = storage_state_path.stat()
        if state_stat.st_size > 10_000_000:
            raise ValidationError("profile storage state exceeds 10 MB")
        if state_stat.st_mode & 0o077:
            raise ValidationError(
                "profile storage state must not be group/world accessible"
            )
        storage_state = json.loads(
            storage_state_path.read_text(encoding="utf-8")
        )
        if not isinstance(storage_state, dict):
            raise ValidationError("profile storage state must be an object")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context_options, resolved_capture = resolve_capture(
            playwright,
            capture=capture,
        )
        context_options.update(
            {
                "accept_downloads": False,
                "service_workers": "block",
                "reduced_motion": "reduce",
            }
        )
        if storage_state is not None:
            context_options["storage_state"] = storage_state
        context = browser.new_context(**context_options)
        page = context.new_page()

        def guard(route: Any) -> None:
            request = route.request
            request_origin = url_origin(request.url)
            if request.method.upper() not in SAFE_HTTP_METHODS:
                route.abort("blockedbyclient")
                return
            if request.resource_type == "websocket":
                route.abort("blockedbyclient")
                return
            if (
                request.resource_type in {"fetch", "xhr"}
                and request_origin != initial_origin
            ):
                route.abort("blockedbyclient")
                return
            if (
                request.is_navigation_request()
                and request_origin not in allowed
            ):
                route.abort("blockedbyclient")
                return
            route.continue_()

        page.route("**/*", guard)
        page.on("popup", lambda popup: popup.close())
        try:
            page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            page.wait_for_timeout(wait_ms)
            final_origin = url_origin(page.url)
            if final_origin not in allowed:
                raise ValidationError(
                    "inspection navigation left allowed origins: "
                    f"{final_origin}"
                )
            if screenshot_path is not None:
                page.screenshot(path=str(screenshot_path), full_page=False)
                os.chmod(screenshot_path, 0o600)
            headings = _visible_items(
                page,
                "h1, h2, h3, h4, h5, h6, [role=heading]",
                kind="heading",
                maximum=maximum_items,
            )
            navigation = _visible_items(
                page,
                "nav a, [role=navigation] a, [role=menuitem]",
                kind="link",
                maximum=maximum_items,
            )
            buttons = _visible_items(
                page,
                "button, [role=button]",
                kind="button",
                maximum=maximum_items,
            )
            links = _visible_items(
                page,
                "a[href], [role=link]",
                kind="link",
                maximum=maximum_items,
            )
            labels = [
                item["text"]
                for item in _visible_items(
                    page,
                    "label",
                    kind="generic",
                    maximum=maximum_items,
                )
            ]
            collections = [headings, navigation, buttons, links, labels]
            remaining = maximum_items
            truncated = False
            for collection in collections:
                if len(collection) > remaining:
                    del collection[remaining:]
                    truncated = True
                remaining -= len(collection)
                if remaining <= 0:
                    remaining = 0
            result = {
                "inspection_id": inspection_id,
                "start_url": safe_public_url(start_url),
                "final_url": safe_public_url(page.url),
                "page_title": page.title(),
                "capture": resolved_capture,
                "headings": headings,
                "navigation": navigation,
                "buttons": buttons,
                "links": links,
                "form_labels": labels,
                "screenshot_path": (
                    str(screenshot_path.resolve())
                    if screenshot_path is not None
                    else None
                ),
                "truncated": truncated,
                "untrusted_content": True,
                "limitations": [
                    "Single page only; no links or controls were activated.",
                    "Unsafe HTTP methods were blocked during inspection.",
                    "Input values, cookies, storage, raw HTML, and URL query "
                    "parameters are not returned.",
                ],
            }
        finally:
            context.close()
            browser.close()
    return result
