"""Video conversion and artifact metadata."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .capture import STANDARD_OUTPUT_SIZE


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
