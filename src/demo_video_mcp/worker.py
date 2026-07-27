"""Detached recording worker for long-running MCP jobs."""

from __future__ import annotations

import argparse
import os
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import sync_playwright

from .capture import resolve_capture
from .config import Settings
from .media import convert_to_mp4, file_metadata, probe_video_size
from .models import is_mutating_step, url_origin
from .plugins import PluginRegistry
from .runner import assert_completion, execute_action
from .storage import JobStore, atomic_write_json, read_json, utc_now


TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_USER"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    return parser.parse_args()


def _manifest(
    *,
    job_id: str,
    plan_hash: str,
    state: str,
    started_at: str,
    steps: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    capture: Dict[str, Any],
    mutation_attempted: bool,
    error: Optional[str],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "plan_hash": plan_hash,
        "state": state,
        "started_at": started_at,
        "finished_at": utc_now(),
        "steps": steps,
        "artifacts": artifacts,
        "capture": capture,
        "external_effect_state": (
            "MUTATION_POSSIBLE" if mutation_attempted else "UNTOUCHED"
        ),
        "review_required": mutation_attempted and state != "SUCCEEDED",
        "error": error,
    }


def run_job(job_id: str) -> int:
    settings = Settings.from_env()
    store = JobStore(settings.data_root)
    status = store.get_status(job_id)
    if status["state"] != "QUEUED":
        return 2
    job_dir = store.job_dir(job_id)
    artifact_dir = job_dir / "artifacts"
    scenario = store.get_scenario(job_id)
    request = read_json(job_dir / "request.json")
    registry = PluginRegistry(settings.plugin_dirs)
    plugin = registry.get(status["plugin_id"])
    plugin_runtime = plugin.load_runtime()
    cancel_path = job_dir / "cancel.request"
    started_at = utc_now()
    store.update_status(
        job_id,
        state="RECORDING",
        started_at=started_at,
        worker_pid=os.getpid(),
    )
    store.append_event(job_id, "recording_started")

    raw_dir = artifact_dir / ".raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    profile_id = request.get("profile_id")
    storage_state = None
    if profile_id:
        storage_state = str(store.profile_dir(profile_id) / "state.json")

    video_object = None
    raw_video_path: Optional[Path] = None
    step_results: list[dict[str, Any]] = []
    mutation_attempted = False
    caught_error: Optional[Exception] = None
    cancelled = False
    resolved_capture: Dict[str, Any] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not bool(scenario.get("headed", False))
        )
        context_arguments, resolved_capture = resolve_capture(
            playwright,
            capture=scenario.get("capture"),
            legacy_viewport=scenario.get("viewport"),
        )
        context_arguments.update(
            {
                "record_video_dir": str(raw_dir),
                "record_video_size": resolved_capture["record_size"],
            }
        )
        if storage_state:
            context_arguments["storage_state"] = storage_state
        context = browser.new_context(**context_arguments)
        page = context.new_page()
        video_object = page.video
        try:
            page.goto(
                scenario["start_url"],
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            allowed_origins = [
                url_origin(item) for item in scenario["allowed_origins"]
            ]
            if url_origin(page.url) not in allowed_origins:
                raise RuntimeError(
                    "initial navigation left allowed origins: "
                    f"{url_origin(page.url)}"
                )
            if "/login" in page.url and profile_id:
                raise RuntimeError(
                    "stored authentication redirected to a login page"
                )
            for step in scenario["steps"]:
                if cancel_path.exists():
                    cancelled = True
                    break
                step_started = utc_now()
                if is_mutating_step(step):
                    mutation_attempted = True
                store.append_event(
                    job_id,
                    "step_started",
                    {
                        "step_id": step["id"],
                        "mutation": is_mutating_step(step),
                    },
                )
                try:
                    execute_action(
                        page,
                        step["action"],
                        allowed_origins=[
                            url_origin(item)
                            for item in scenario["allowed_origins"]
                        ],
                        artifact_dir=artifact_dir,
                        plugin_runtime=plugin_runtime,
                    )
                    if url_origin(page.url) not in allowed_origins:
                        raise RuntimeError(
                            "step navigation left allowed origins: "
                            f"{url_origin(page.url)}"
                        )
                    for assertion in step.get("completion", []):
                        assert_completion(
                            page,
                            assertion,
                            timeout_ms=int(
                                step.get("timeout_ms", 30_000)
                            ),
                        )
                    hold_ms = int(step.get("hold_ms", 800))
                    if hold_ms:
                        page.wait_for_timeout(hold_ms)
                    result = {
                        "step_id": step["id"],
                        "state": "passed",
                        "started_at": step_started,
                        "finished_at": utc_now(),
                        "error": None,
                    }
                    step_results.append(result)
                    store.append_event(
                        job_id,
                        "step_completed",
                        {"step_id": step["id"]},
                    )
                except Exception as error:
                    result = {
                        "step_id": step["id"],
                        "state": "failed",
                        "started_at": step_started,
                        "finished_at": utc_now(),
                        "error": str(error),
                    }
                    step_results.append(result)
                    raise
        except Exception as error:
            caught_error = error
            try:
                page.screenshot(
                    path=str(artifact_dir / "error.png"),
                    full_page=False,
                )
            except Exception:
                pass
        finally:
            try:
                page.close()
            finally:
                context.close()
                if video_object is not None:
                    raw_video_path = Path(video_object.path())
                browser.close()

    store.update_status(job_id, state="FINALIZING")
    artifacts: list[dict[str, Any]] = []
    if raw_video_path is not None and raw_video_path.is_file():
        webm_path = artifact_dir / "recording.webm"
        raw_video_path.replace(webm_path)
        partial = bool(caught_error or cancelled)
        artifacts.append(file_metadata(webm_path, partial=partial))
        mp4_path = artifact_dir / "video.mp4"
        try:
            if not convert_to_mp4(webm_path, mp4_path):
                raise RuntimeError(
                    "FFmpeg is unavailable; required MP4 was not created"
                )
            actual_size = probe_video_size(mp4_path)
            if actual_size != resolved_capture["output_size"]:
                raise RuntimeError(
                    "MP4 output size mismatch: expected "
                    f"{resolved_capture['output_size']}, got {actual_size}"
                )
            artifacts.append(file_metadata(mp4_path, partial=partial))
        except Exception as error:
            store.append_event(
                job_id,
                "mp4_conversion_failed",
                {"error": str(error)},
            )
            if caught_error is None:
                caught_error = RuntimeError(
                    f"MP4 conversion failed: {error}"
                )
    elif caught_error is None:
        caught_error = RuntimeError(
            "Playwright did not produce a recording artifact"
        )

    if cancelled:
        final_state = "CANCELLED"
        error_text = None
    elif caught_error and mutation_attempted:
        final_state = "NEEDS_USER"
        error_text = str(caught_error)
    elif caught_error:
        final_state = "FAILED"
        error_text = str(caught_error)
    else:
        final_state = "SUCCEEDED"
        error_text = None
    manifest = _manifest(
        job_id=job_id,
        plan_hash=status["plan_hash"],
        state=final_state,
        started_at=started_at,
        steps=step_results,
        artifacts=artifacts,
        capture=resolved_capture,
        mutation_attempted=mutation_attempted,
        error=error_text,
    )
    atomic_write_json(job_dir / "manifest.json", manifest)
    store.update_status(
        job_id,
        state=final_state,
        finished_at=manifest["finished_at"],
        error=error_text,
    )
    store.append_event(
        job_id,
        "recording_finished",
        {"state": final_state},
    )
    return 0 if final_state == "SUCCEEDED" else 1


def main() -> None:
    args = _parse_args()
    settings = Settings.from_env()
    store = JobStore(settings.data_root)
    try:
        raise SystemExit(run_job(args.job_id))
    except SystemExit:
        raise
    except Exception as error:
        traceback.print_exc()
        try:
            store.update_status(
                args.job_id,
                state="FAILED",
                error=str(error),
                finished_at=utc_now(),
            )
            store.append_event(
                args.job_id,
                "worker_crashed",
                {"error": str(error)},
            )
        except Exception:
            traceback.print_exc()
        raise SystemExit(1)
    finally:
        lock_path = settings.data_root / "recording.lock"
        try:
            if (
                lock_path.is_file()
                and lock_path.read_text(encoding="utf-8").strip()
                == args.job_id
            ):
                lock_path.unlink()
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    main()
