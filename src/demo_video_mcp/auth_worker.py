"""Detached headed-browser worker for interactive login capture."""

from __future__ import annotations

import argparse
import os
import time
import traceback

from playwright.sync_api import sync_playwright

from .capture import resolve_capture
from .config import Settings
from .models import safe_public_url, url_origin
from .storage import JobStore, atomic_write_json, read_json, utc_now


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    return parser.parse_args()


def run_auth_session(session_id: str, timeout_seconds: int) -> int:
    settings = Settings.from_env()
    store = JobStore(settings.data_root)
    status = store.get_auth_status(session_id)
    session_dir = store.auth_session_dir(session_id)
    request_path = session_dir / "request.json"
    request = read_json(request_path) if request_path.is_file() else status
    finish_path = session_dir / "finish.request"
    cancel_path = session_dir / "cancel.request"
    profile_dir = store.profile_dir(status["profile_id"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context_options, resolved_capture = resolve_capture(
                playwright,
                capture=status.get("capture"),
            )
            context = browser.new_context(**context_options)
            page = context.new_page()
            page.goto(
                request["login_url"],
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            store.update_auth_status(
                session_id,
                state="BROWSER_OPEN",
                worker_pid=os.getpid(),
                current_url=safe_public_url(page.url),
            )
            final_state = None
            while time.monotonic() < deadline:
                if cancel_path.exists():
                    final_state = "CANCELLED"
                    break
                if finish_path.exists():
                    state_path = profile_dir / "state.json"
                    context.storage_state(path=str(state_path))
                    os.chmod(state_path, 0o600)
                    metadata = {
                        "profile_id": status["profile_id"],
                        "login_url": status["login_url"],
                        "current_url": safe_public_url(page.url),
                        "origin": url_origin(page.url) or None,
                        "page_title": page.title(),
                        "capture": resolved_capture,
                        "created_at": utc_now(),
                    }
                    atomic_write_json(
                        profile_dir / "profile.json",
                        metadata,
                        mode=0o600,
                    )
                    final_state = "COMPLETED"
                    break
                store.update_auth_status(
                    session_id,
                    current_url=safe_public_url(page.url),
                )
                time.sleep(0.25)
            if final_state is None:
                final_state = "TIMED_OUT"
            final_url = safe_public_url(page.url)
            context.close()
            browser.close()
            store.update_auth_status(
                session_id,
                state=final_state,
                current_url=final_url,
            )
            return 0 if final_state == "COMPLETED" else 1
    except Exception as error:
        store.update_auth_status(
            session_id,
            state="FAILED",
            error=str(error),
        )
        return 1


def main() -> None:
    args = _parse_args()
    try:
        raise SystemExit(
            run_auth_session(args.session_id, args.timeout_seconds)
        )
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
