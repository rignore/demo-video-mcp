"""Filesystem-backed profiles, jobs, audit events, and artifacts."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .errors import NotFoundError, ValidationError
from .models import safe_public_url


SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
AUTH_ERROR_MESSAGE = (
    "Interactive login failed. Check the private worker log."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(
            f"{label} must use letters, digits, underscore, or hyphen"
        )
    return value


def atomic_write_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    mode: int = 0o644,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise NotFoundError(f"file not found: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValidationError(f"expected JSON object: {path.name}")
    return value


def contained_path(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.expanduser().resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValidationError(
            f"path is outside configured root: {candidate}"
        ) from error
    return resolved_candidate


def allowed_path(path: Path, roots: Iterable[Path]) -> Path:
    resolved = path.expanduser().resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue
    raise ValidationError(f"path is outside allowed roots: {path}")


class JobStore:
    def __init__(self, data_root: Path):
        self.data_root = data_root.resolve()
        self.jobs_root = self.data_root / "jobs"
        self.profiles_root = self.data_root / "profiles"
        self.auth_sessions_root = self.data_root / "auth-sessions"
        self.native_apps_root = self.data_root / "native-apps"
        for path in (
            self.jobs_root,
            self.profiles_root,
            self.auth_sessions_root,
            self.native_apps_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.native_apps_root, 0o700)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / validate_id(job_id, "job_id")

    def profile_dir(self, profile_id: str) -> Path:
        return self.profiles_root / validate_id(profile_id, "profile_id")

    def auth_session_dir(self, session_id: str) -> Path:
        return self.auth_sessions_root / validate_id(session_id, "session_id")

    def native_app_dir(self, artifact_id: str) -> Path:
        return self.native_apps_root / validate_id(
            artifact_id,
            "artifact_id",
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def register_native_app(
        self,
        *,
        source: Path,
        platform: str,
        artifact_format: str,
        sha256: str,
        size_bytes: int,
    ) -> Dict[str, Any]:
        for existing in self.list_native_apps():
            if (
                existing.get("platform") == platform
                and existing.get("format") == artifact_format
                and existing.get("sha256") == sha256
            ):
                return existing

        artifact_id = uuid.uuid4().hex
        artifact_dir = self.native_app_dir(artifact_id)
        artifact_dir.mkdir(mode=0o700)
        suffix = ".apk" if artifact_format == "apk" else ".app.zip"
        destination = artifact_dir / f"app{suffix}"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".app.",
            dir=str(artifact_dir),
        )
        temporary = Path(temporary_name)
        try:
            with (
                source.open("rb") as input_handle,
                os.fdopen(descriptor, "wb") as output_handle,
            ):
                shutil.copyfileobj(
                    input_handle,
                    output_handle,
                    length=1024 * 1024,
                )
                output_handle.flush()
                os.fsync(output_handle.fileno())
            os.chmod(temporary, 0o600)
            copied_size = temporary.stat().st_size
            copied_sha256 = self._file_sha256(temporary)
            if copied_size != size_bytes or copied_sha256 != sha256:
                raise ValidationError(
                    "native app changed while it was being registered"
                )
            os.replace(temporary, destination)
            metadata = {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "platform": platform,
                "format": artifact_format,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "registered_at": utc_now(),
            }
            atomic_write_json(
                artifact_dir / "artifact.json",
                metadata,
                mode=0o600,
            )
            return {
                **metadata,
                "path": str(destination.resolve()),
            }
        except Exception:
            if temporary.exists():
                temporary.unlink()
            if destination.exists():
                destination.unlink()
            metadata_path = artifact_dir / "artifact.json"
            if metadata_path.exists():
                metadata_path.unlink()
            try:
                artifact_dir.rmdir()
            except OSError:
                pass
            raise

    def get_native_app(self, artifact_id: str) -> Dict[str, Any]:
        artifact_dir = self.native_app_dir(artifact_id)
        metadata = read_json(artifact_dir / "artifact.json")
        artifact_format = metadata.get("format")
        if artifact_format == "apk":
            app_path = artifact_dir / "app.apk"
        elif artifact_format == "app_zip":
            app_path = artifact_dir / "app.app.zip"
        else:
            raise ValidationError("native app format is invalid")
        if not app_path.is_file() or app_path.is_symlink():
            raise NotFoundError("registered native app file is unavailable")
        if app_path.stat().st_mode & 0o077:
            os.chmod(app_path, 0o600)
        metadata_path = artifact_dir / "artifact.json"
        if metadata_path.stat().st_mode & 0o077:
            os.chmod(metadata_path, 0o600)
        return {
            **metadata,
            "path": str(app_path.resolve()),
        }

    def list_native_apps(self) -> List[Dict[str, Any]]:
        apps: List[Dict[str, Any]] = []
        if not self.native_apps_root.is_dir():
            return apps
        for path in sorted(self.native_apps_root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            try:
                apps.append(self.get_native_app(path.name))
            except (NotFoundError, ValidationError):
                continue
        return apps

    def create_job(
        self,
        *,
        plugin_id: str,
        scenario: Mapping[str, Any],
        request: Mapping[str, Any],
        plan_hash: str,
        summary: Mapping[str, Any],
    ) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex
        job_dir = self.job_dir(job_id)
        (job_dir / "input" / "guides").mkdir(parents=True)
        (job_dir / "artifacts").mkdir()
        atomic_write_json(job_dir / "scenario.json", scenario)
        atomic_write_json(job_dir / "request.json", request)
        status = {
            "job_id": job_id,
            "plugin_id": plugin_id,
            "state": "DRAFT",
            "plan_hash": plan_hash,
            "summary": dict(summary),
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "approved_at": None,
            "started_at": None,
            "finished_at": None,
            "worker_pid": None,
            "error": None,
        }
        atomic_write_json(job_dir / "status.json", status)
        self.append_event(job_id, "job_created", {"plan_hash": plan_hash})
        return status

    def get_status(self, job_id: str) -> Dict[str, Any]:
        return read_json(self.job_dir(job_id) / "status.json")

    def get_scenario(self, job_id: str) -> Dict[str, Any]:
        return read_json(self.job_dir(job_id) / "scenario.json")

    def update_status(
        self, job_id: str, **changes: Any
    ) -> Dict[str, Any]:
        path = self.job_dir(job_id) / "status.json"
        status = read_json(path)
        status.update(changes)
        status["updated_at"] = utc_now()
        atomic_write_json(path, status)
        return status

    def append_event(
        self, job_id: str, event: str, data: Optional[Mapping[str, Any]] = None
    ) -> None:
        path = self.job_dir(job_id) / "events.jsonl"
        record = {
            "at": utc_now(),
            "event": event,
            "data": dict(data or {}),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            )

    def list_artifacts(self, job_id: str) -> List[Dict[str, Any]]:
        artifact_root = self.job_dir(job_id) / "artifacts"
        artifacts = []
        if not artifact_root.is_dir():
            return artifacts
        for path in sorted(artifact_root.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            artifacts.append(
                {
                    "artifact_id": path.name,
                    "path": str(path.resolve()),
                    "size_bytes": path.stat().st_size,
                }
            )
        return artifacts

    def create_auth_session(
        self,
        profile_id: str,
        login_url: str,
        capture: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        validate_id(profile_id, "profile_id")
        session_id = uuid.uuid4().hex
        session_dir = self.auth_session_dir(session_id)
        session_dir.mkdir(parents=True)
        status = {
            "session_id": session_id,
            "profile_id": profile_id,
            "login_url": safe_public_url(login_url),
            "capture": dict(capture or {}),
            "state": "STARTING",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "worker_pid": None,
            "current_url": None,
            "error": None,
        }
        atomic_write_json(
            session_dir / "request.json",
            {"login_url": login_url},
            mode=0o600,
        )
        atomic_write_json(
            session_dir / "status.json",
            status,
            mode=0o600,
        )
        return status

    def get_auth_status(self, session_id: str) -> Dict[str, Any]:
        path = self.auth_session_dir(session_id) / "status.json"
        status = read_json(path)
        sanitized = dict(status)
        for field in ("login_url", "current_url"):
            value = sanitized.get(field)
            if isinstance(value, str):
                sanitized[field] = safe_public_url(value)
        if sanitized.get("error"):
            sanitized["error"] = AUTH_ERROR_MESSAGE
        if sanitized != status or path.stat().st_mode & 0o077:
            atomic_write_json(path, sanitized, mode=0o600)
        return sanitized

    def update_auth_status(
        self, session_id: str, **changes: Any
    ) -> Dict[str, Any]:
        path = self.auth_session_dir(session_id) / "status.json"
        status = read_json(path)
        for field in ("login_url", "current_url"):
            value = changes.get(field)
            if isinstance(value, str):
                changes[field] = safe_public_url(value)
        if changes.get("error"):
            changes["error"] = AUTH_ERROR_MESSAGE
        status.update(changes)
        status["updated_at"] = utc_now()
        atomic_write_json(path, status, mode=0o600)
        return status

    def get_profile(self, profile_id: str) -> Dict[str, Any]:
        profile_dir = self.profile_dir(profile_id)
        metadata_path = profile_dir / "profile.json"
        metadata = read_json(metadata_path)
        sanitized = dict(metadata)
        for field in ("login_url", "current_url"):
            value = sanitized.get(field)
            if isinstance(value, str):
                sanitized[field] = safe_public_url(value)
        if (
            sanitized != metadata
            or metadata_path.stat().st_mode & 0o077
        ):
            atomic_write_json(metadata_path, sanitized, mode=0o600)
        metadata = sanitized
        state_path = profile_dir / "state.json"
        metadata["auth_state_available"] = state_path.is_file()
        return metadata
