"""Detached Android native-app recording worker."""

from __future__ import annotations

import argparse
import hashlib
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from .capture import STANDARD_OUTPUT_SIZE
from .captions import caption_summary
from .config import Settings
from .media import (
    convert_to_mp4,
    file_metadata,
    generate_caption_artifacts,
    probe_video_size,
)
from .models import is_mutating_step
from .native_models import validate_native_scenario
from .native_runtime import (
    build_android_capabilities,
    ensure_appium_server,
    execute_native_action,
)
from .storage import JobStore, atomic_write_json, read_json, utc_now


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(
    *,
    job_id: str,
    plan_hash: str,
    state: str,
    started_at: str,
    steps: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    scenario: Dict[str, Any],
    captions: Dict[str, Any],
    mutation_attempted: bool,
    error: Optional[str],
) -> Dict[str, Any]:
    device = dict(scenario["device"])
    return {
        "schema_version": 1,
        "job_id": job_id,
        "plan_hash": plan_hash,
        "backend": "native-android",
        "state": state,
        "started_at": started_at,
        "finished_at": utc_now(),
        "steps": steps,
        "artifacts": artifacts,
        "captions": captions,
        "capture": {
            "target": "native",
            "platform": "android",
            "runtime": "emulator",
            "device": device,
            "orientation": device.get("orientation", "portrait"),
            "output_size": dict(STANDARD_OUTPUT_SIZE),
        },
        "app": {
            "artifact_id": scenario["app_artifact_id"],
            "package_id": scenario["package_id"],
        },
        "external_effect_state": (
            "MUTATION_POSSIBLE" if mutation_attempted else "UNTOUCHED"
        ),
        "review_required": mutation_attempted and state != "SUCCEEDED",
        "error": error,
    }


def run_native_job(job_id: str) -> int:
    settings = Settings.from_env()
    store = JobStore(settings.data_root)
    status = store.get_status(job_id)
    if status["state"] != "QUEUED":
        return 2
    job_dir = store.job_dir(job_id)
    artifact_dir = job_dir / "artifacts"
    scenario = store.get_scenario(job_id)
    request = read_json(job_dir / "request.json")
    if request.get("backend") != "native-android":
        raise RuntimeError("job is not an Android native recording")
    errors = validate_native_scenario(scenario)
    if errors:
        raise RuntimeError("frozen native scenario is invalid")
    app = store.get_native_app(scenario["app_artifact_id"])
    app_path = Path(app["path"])
    if (
        app.get("sha256") != request.get("artifact_sha256")
        or _sha256(app_path) != request.get("artifact_sha256")
    ):
        raise RuntimeError("registered app artifact changed after preflight")

    cancel_path = job_dir / "cancel.request"
    started_at = utc_now()
    store.update_status(
        job_id,
        state="RECORDING",
        started_at=started_at,
        worker_pid=os.getpid(),
    )
    store.append_event(job_id, "native_recording_started")

    session_id: Optional[str] = None
    raw_recording: Optional[bytes] = None
    recording_started = False
    step_results: list[dict[str, Any]] = []
    mutation_attempted = False
    caught_error: Optional[Exception] = None
    cancelled = False
    recording_clock = 0.0

    try:
        with ensure_appium_server(
            settings,
            job_dir / "appium.log",
        ) as client:
            session_id, _ = client.create_session(
                build_android_capabilities(
                    scenario,
                    app_path,
                    auto_launch=False,
                )
            )
            try:
                client.set_orientation(
                    session_id,
                    scenario["device"].get(
                        "orientation",
                        "portrait",
                    ),
                )
                client.start_recording(
                    session_id,
                    int(scenario.get("max_duration_seconds", 180)),
                )
                recording_started = True
                recording_clock = time.monotonic()
                for step in scenario["steps"]:
                    if cancel_path.exists():
                        cancelled = True
                        break
                    step_started = utc_now()
                    mutating = is_mutating_step(step)
                    if mutating:
                        mutation_attempted = True
                    store.append_event(
                        job_id,
                        "step_started",
                        {
                            "step_id": step["id"],
                            "mutation": mutating,
                            "backend": "native-android",
                        },
                    )
                    try:
                        execute_native_action(
                            client,
                            session_id,
                            step["action"],
                            package_id=scenario["package_id"],
                            artifact_dir=artifact_dir,
                            timeout_ms=int(
                                step.get("timeout_ms", 30_000)
                            ),
                        )
                        scene_visible_clock = time.monotonic()
                        hold_ms = int(step.get("hold_ms", 800))
                        if hold_ms:
                            time.sleep(hold_ms / 1000)
                        scene_finished_clock = time.monotonic()
                        video_start_ms = max(
                            0,
                            round(
                                (
                                    scene_visible_clock
                                    - recording_clock
                                )
                                * 1000
                            ),
                        )
                        video_end_ms = max(
                            video_start_ms + 1,
                            round(
                                (
                                    scene_finished_clock
                                    - recording_clock
                                )
                                * 1000
                            ),
                        )
                        result = {
                            "step_id": step["id"],
                            "state": "passed",
                            "started_at": step_started,
                            "finished_at": utc_now(),
                            "video_start_ms": video_start_ms,
                            "video_end_ms": video_end_ms,
                            "error": None,
                        }
                        step_results.append(result)
                        store.append_event(
                            job_id,
                            "step_completed",
                            {"step_id": step["id"]},
                        )
                    except Exception as error:
                        step_results.append(
                            {
                                "step_id": step["id"],
                                "state": "failed",
                                "started_at": step_started,
                                "finished_at": utc_now(),
                                "error": str(error),
                            }
                        )
                        raise
            except Exception as error:
                caught_error = error
                try:
                    error_path = artifact_dir / "error.png"
                    error_path.write_bytes(client.screenshot(session_id))
                    os.chmod(error_path, 0o600)
                except Exception:
                    pass
            finally:
                if recording_started:
                    try:
                        raw_recording = client.stop_recording(session_id)
                    except Exception as error:
                        if caught_error is None:
                            caught_error = error
                client.delete_session(session_id)
    except Exception as error:
        if caught_error is None:
            caught_error = error

    store.update_status(job_id, state="FINALIZING")
    artifacts: list[dict[str, Any]] = []
    captions = {**caption_summary(scenario), "cues": []}
    mp4_path: Optional[Path] = None
    if raw_recording:
        raw_path = artifact_dir / "native-recording.mp4"
        raw_path.write_bytes(raw_recording)
        os.chmod(raw_path, 0o600)
        partial = bool(caught_error or cancelled)
        artifacts.append(file_metadata(raw_path, partial=partial))
        mp4_path = artifact_dir / "video.mp4"
        try:
            if not convert_to_mp4(raw_path, mp4_path):
                raise RuntimeError(
                    "FFmpeg is unavailable; required MP4 was not created"
                )
            actual_size = probe_video_size(mp4_path)
            if actual_size != STANDARD_OUTPUT_SIZE:
                raise RuntimeError(
                    "MP4 output size mismatch: expected "
                    f"{STANDARD_OUTPUT_SIZE}, got {actual_size}"
                )
            os.chmod(mp4_path, 0o600)
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
            "Appium did not produce a recording artifact"
        )

    if (
        mp4_path is not None
        and mp4_path.is_file()
        and caught_error is None
        and not cancelled
    ):
        try:
            caption_outputs = generate_caption_artifacts(
                scenario,
                step_results,
                mp4_path,
                artifact_dir,
            )
            captions = caption_outputs["manifest"]
            for artifact in caption_outputs["artifacts"]:
                Path(artifact["path"]).chmod(0o600)
            artifacts.extend(caption_outputs["artifacts"])
        except Exception as error:
            store.append_event(
                job_id,
                "caption_generation_failed",
                {"error": str(error)},
            )
            caught_error = RuntimeError(
                f"caption generation failed: {error}"
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
        scenario=scenario,
        captions=captions,
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
        "native_recording_finished",
        {"state": final_state},
    )
    return 0 if final_state == "SUCCEEDED" else 1


def main() -> None:
    args = _parse_args()
    settings = Settings.from_env()
    store = JobStore(settings.data_root)
    try:
        raise SystemExit(run_native_job(args.job_id))
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
                "native_worker_crashed",
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
