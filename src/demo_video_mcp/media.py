"""Video conversion and artifact metadata."""

from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .capture import STANDARD_OUTPUT_SIZE
from .captions import (
    caption_cues,
    caption_review_required,
    caption_summary,
    render_webvtt,
)


def find_ffmpeg() -> Optional[str]:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    return imageio_ffmpeg.get_ffmpeg_exe()


def convert_to_mp4(webm_path: Path, mp4_path: Path) -> bool:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(webm_path),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-vf",
        (
            f"scale={STANDARD_OUTPUT_SIZE['width']}:"
            f"{STANDARD_OUTPUT_SIZE['height']}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={STANDARD_OUTPUT_SIZE['width']}:"
            f"{STANDARD_OUTPUT_SIZE['height']}:"
            "(ow-iw)/2:(oh-ih)/2,setsar=1"
        ),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(mp4_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return True


def probe_video_size(path: Path) -> Optional[Dict[str, int]]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    match = re.search(
        r"Video:.*?(\d{2,5})x(\d{2,5})",
        result.stderr,
    )
    if not match:
        return None
    return {
        "width": int(match.group(1)),
        "height": int(match.group(2)),
    }


def probe_video_duration(path: Path) -> Optional[float]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    match = re.search(
        r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)",
        result.stderr,
    )
    if not match:
        return None
    return (
        int(match.group(1)) * 3600
        + int(match.group(2)) * 60
        + float(match.group(3))
    )


def burn_caption_cues(
    video_path: Path,
    cues: Sequence[Mapping[str, Any]],
    output_path: Path,
) -> None:
    """Render caption cards and burn them into a Full HD MP4."""

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for burned-in captions")
    if not cues:
        raise RuntimeError("at least one caption cue is required")
    duration = probe_video_duration(video_path)
    if duration is None or duration <= 0:
        raise RuntimeError("could not determine source video duration")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Playwright Chromium is required to render caption text"
        ) from error

    with tempfile.TemporaryDirectory(
        prefix=".caption-overlays-",
        dir=output_path.parent,
    ) as temporary:
        overlay_dir = Path(temporary)
        overlay_paths = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={
                        "width": STANDARD_OUTPUT_SIZE["width"],
                        "height": STANDARD_OUTPUT_SIZE["height"],
                    }
                )
                for index, cue in enumerate(cues, start=1):
                    text = html.escape(str(cue["text"]).strip())
                    page.set_content(
                        """
                        <style>
                          html, body {
                            margin: 0;
                            width: 1920px;
                            height: 1080px;
                            background: transparent;
                            overflow: hidden;
                          }
                          body {
                            display: flex;
                            align-items: flex-end;
                            justify-content: center;
                            box-sizing: border-box;
                            padding: 0 120px 72px;
                          }
                          #caption {
                            max-width: 1560px;
                            padding: 18px 32px 20px;
                            border-radius: 12px;
                            background: rgba(0, 0, 0, 0.78);
                            color: white;
                            font: 600 38px/1.45 -apple-system,
                              BlinkMacSystemFont, "Apple SD Gothic Neo",
                              "Noto Sans CJK KR", sans-serif;
                            letter-spacing: -0.3px;
                            text-align: center;
                            white-space: pre-wrap;
                            overflow-wrap: anywhere;
                          }
                        </style>
                        <div id="caption">"""
                        + text
                        + "</div>"
                    )
                    overlay_path = overlay_dir / f"{index:03d}.png"
                    page.screenshot(
                        path=str(overlay_path),
                        omit_background=True,
                    )
                    overlay_paths.append(overlay_path)
            finally:
                browser.close()

        command = [ffmpeg, "-y", "-i", str(video_path)]
        for overlay_path in overlay_paths:
            command.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    "1",
                    "-i",
                    str(overlay_path),
                ]
            )
        filters = []
        previous = "[0:v]"
        for index, cue in enumerate(cues, start=1):
            output_label = f"[captioned{index}]"
            start = int(cue["start_ms"]) / 1000
            end = int(cue["end_ms"]) / 1000
            filters.append(
                f"{previous}[{index}:v]overlay=0:0:"
                f"shortest=1:eof_action=repeat:"
                f"enable='between(t,{start:.3f},"
                f"{end:.3f})'{output_label}"
            )
            previous = output_label
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                previous,
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "copy",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-t",
                f"{duration:.3f}",
                "-shortest",
                str(output_path),
            ]
        )
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )


def generate_caption_artifacts(
    scenario: Mapping[str, Any],
    step_results: Sequence[Mapping[str, Any]],
    video_path: Path,
    artifact_dir: Path,
) -> Dict[str, Any]:
    """Create approved timed caption outputs and manifest metadata."""

    summary = caption_summary(scenario)
    if not caption_review_required(scenario):
        return {
            "manifest": {**summary, "cues": []},
            "artifacts": [],
        }

    cues = caption_cues(scenario, step_results)
    if not cues:
        raise RuntimeError(
            "captioned recording completed without usable scene timings"
        )

    output = summary["output"]
    webvtt_path = artifact_dir / "captions.vtt"
    webvtt_path.write_text(render_webvtt(cues), encoding="utf-8")
    artifacts = []
    sidecar_name = None
    burned_in_name = None

    if output in {"sidecar", "both"}:
        artifacts.append(file_metadata(webvtt_path))
        sidecar_name = webvtt_path.name

    if output in {"burned_in", "both"}:
        captioned_video_path = artifact_dir / "video-captioned.mp4"
        burn_caption_cues(
            video_path,
            cues,
            captioned_video_path,
        )
        artifacts.append(file_metadata(captioned_video_path))
        burned_in_name = captioned_video_path.name

    if output == "burned_in":
        webvtt_path.unlink(missing_ok=True)

    return {
        "manifest": {
            **summary,
            "cues": cues,
            "sidecar": sidecar_name,
            "burned_in_video": burned_in_name,
        },
        "artifacts": artifacts,
    }


def file_metadata(path: Path, *, partial: bool = False) -> Dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "partial": partial,
    }
