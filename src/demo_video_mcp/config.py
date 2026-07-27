"""Runtime configuration with conservative local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Tuple


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[1]


def _resolved_paths(raw: str | None, defaults: Iterable[Path]) -> Tuple[Path, ...]:
    values = raw.split(os.pathsep) if raw else [str(path) for path in defaults]
    unique = []
    for value in values:
        if not value:
            continue
        resolved = Path(value).expanduser().resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_root: Path
    plugin_dirs: Tuple[Path, ...]
    allowed_guide_roots: Tuple[Path, ...]
    allow_file_urls: bool
    appium_server_url: str = "http://127.0.0.1:4723"
    native_app_max_bytes: int = 2_000_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(
            os.environ.get("DEMO_VIDEO_PROJECT_ROOT", str(PROJECT_ROOT))
        ).expanduser().resolve()
        data_root = Path(
            os.environ.get(
                "DEMO_VIDEO_DATA_ROOT",
                str(project_root / ".demo-video-data"),
            )
        ).expanduser().resolve()
        plugin_dirs = _resolved_paths(
            os.environ.get("DEMO_VIDEO_PLUGIN_DIRS"),
            [project_root / "plugins"],
        )
        guide_roots = _resolved_paths(
            os.environ.get("DEMO_VIDEO_ALLOWED_ROOTS"),
            [project_root],
        )
        return cls(
            project_root=project_root,
            data_root=data_root,
            plugin_dirs=plugin_dirs,
            allowed_guide_roots=guide_roots,
            allow_file_urls=(
                os.environ.get("DEMO_VIDEO_ALLOW_FILE_URLS", "") == "1"
            ),
            appium_server_url=os.environ.get(
                "DEMO_VIDEO_APPIUM_URL",
                "http://127.0.0.1:4723",
            ),
            native_app_max_bytes=int(
                os.environ.get(
                    "DEMO_VIDEO_NATIVE_APP_MAX_BYTES",
                    "2000000000",
                )
            ),
        )

    def ensure_runtime_dirs(self) -> None:
        for directory in (
            self.data_root,
            self.data_root / "jobs",
            self.data_root / "profiles",
            self.data_root / "auth-sessions",
            self.data_root / "native-apps",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_root / "native-apps", 0o700)
