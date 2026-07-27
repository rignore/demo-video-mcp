"""Trusted Protect GO-only operations.

This module is loaded only when the optional Protect GO plugin directory is
configured. No Protect GO selector or workflow rule is imported by the core.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from playwright.sync_api import Page


def _hold(page: Page, milliseconds: int = 800) -> None:
    page.wait_for_timeout(milliseconds)


def _close_notification_center(page: Page, _: Mapping[str, Any]) -> None:
    heading = page.get_by_role("heading", name="알림 센터")
    header = heading.locator("xpath=..")
    buttons = header.locator(":scope > button")
    if buttons.count() != 1:
        raise RuntimeError("알림 센터 close 버튼을 찾지 못했습니다.")
    buttons.click()
    _hold(page)
    drawer = heading.locator("xpath=../../..")
    drawer_class = drawer.get_attribute("class") or ""
    if "_drawer_open_" in drawer_class:
        raise RuntimeError("알림 센터 drawer가 닫히지 않았습니다.")


def _select_recommended_tag(
    page: Page, params: Mapping[str, Any]
) -> None:
    step = params.get("step", "?")
    panel = page.get_by_role("complementary")
    recommended = panel.get_by_role(
        "button",
        name=re.compile(r"AI 추천$"),
    )
    if recommended.count() != 1:
        raise RuntimeError(
            f"{step}단계 AI 추천 태그가 1개가 아닙니다: "
            f"{recommended.count()}개"
        )
    selected_class = recommended.get_attribute("class") or ""
    if "_selected_" not in selected_class:
        recommended.click()
        _hold(page)
        selected_class = recommended.get_attribute("class") or ""
    if "_selected_" not in selected_class:
        raise RuntimeError(
            f"{step}단계 AI 추천 태그 선택이 반영되지 않았습니다."
        )


def execute_operation(
    page: Page,
    operation: str,
    params: Mapping[str, Any],
) -> None:
    operations = {
        "close_notification_center": _close_notification_center,
        "select_recommended_tag": _select_recommended_tag,
    }
    try:
        handler = operations[operation]
    except KeyError as error:
        raise RuntimeError(
            f"unsupported Protect GO operation: {operation}"
        ) from error
    handler(page, params)
