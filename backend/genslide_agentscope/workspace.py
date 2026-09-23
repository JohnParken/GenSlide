"""Bounded, request-owned temporary workspaces."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .domain import ServiceError


class DiskUsage(NamedTuple):
    used_bytes: int
    free_bytes: int


@dataclass
class Workspace:
    path: Path
    inputs: Path
    scratch: Path
    outputs: Path
    _manager: "WorkspaceManager"
    _lock_file: object

    def check_usage(self) -> DiskUsage:
        return self._manager.check_usage(self.path)

    def cleanup(self) -> None:
        self._manager._cleanup(self)


class WorkspaceManager:
    _MANIFEST = ".genslide-workspace.json"
    _LOCK = ".workspace.lock"
    _FAILURES = ".cleanup-failures.log"

    def __init__(self, root: Path | str, max_bytes: int, min_free_bytes: int, stale_seconds: float,
                 *, request_max_bytes: int = 128 * 1024 * 1024):
        self.root = Path(root).absolute()
        self.max_bytes = max(0, int(max_bytes))
        self.request_max_bytes = max(0, int(request_max_bytes))
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.stale_seconds = float(stale_seconds)
        current = Path(self.root.anchor)
        for part in self.root.parts[1:]:
            current /= part
            try:
                if current.is_symlink():
                    raise ServiceError("WORKSPACE_INVALID", 500)
            except OSError as exc:
                raise ServiceError("WORKSPACE_INVALID", 500) from exc
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ServiceError("WORKSPACE_INVALID", 500)
        os.chmod(self.root, 0o700)

    def check_usage(self, request_path: Path | None = None) -> DiskUsage:
        usage = shutil.disk_usage(self.root)
        if request_path is not None and (not self._owned(request_path) or request_path.is_symlink()):
            raise ServiceError("WORKSPACE_INVALID", 500)
        used = 0
        request_used = 0
        for base, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(base, d).is_symlink())]
            for name in files:
                path = Path(base, name)
                try:
                    if not path.is_symlink():
                        size = path.stat().st_size
                        used += size
                        if request_path is not None and path.is_relative_to(request_path):
                            request_used += size
                except OSError:
                    continue
        result = DiskUsage(used, usage.free)
        if request_used > self.request_max_bytes:
            raise ServiceError("WORKSPACE_REQUEST_CAPACITY", 507)
        if result.used_bytes > self.max_bytes or result.free_bytes < self.min_free_bytes:
            raise ServiceError("WORKSPACE_CAPACITY", 507)
        return result

    def _open_lock(self, path: Path, create: bool = False):
        flags = os.O_RDWR | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        return os.fdopen(os.open(path, flags, 0o600), "a+")

    def create(self, action_id: str, instance_id: str) -> Workspace:
        self.check_usage()
        for _ in range(10):
            name = f"ws-{secrets.token_hex(12)}"
            path = self.root / name
            try:
                path.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise ServiceError("WORKSPACE_UNAVAILABLE", 503)
        try:
            os.chmod(path, 0o700)
            lock_file = self._open_lock(path / self._LOCK, create=True)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for child in ("inputs", "scratch", "outputs"):
                (path / child).mkdir(mode=0o700)
                os.chmod(path / child, 0o700)
            manifest = {
                "version": 1,
                "created_at": time.time(),
                "action_id": str(action_id),
                "instance_id": str(instance_id),
            }
            with open(path / self._MANIFEST, "x", encoding="utf-8") as stream:
                json.dump(manifest, stream, separators=(",", ":"))
            self.check_usage()
            return Workspace(path, path / "inputs", path / "scratch", path / "outputs", self, lock_file)
        except Exception:
            try:
                lock_file.close()  # type: ignore[union-attr]
            except (UnboundLocalError, OSError):
                pass
            self._remove_tree(path)
            raise

    def _owned(self, path: Path) -> bool:
        try:
            manifest = path / self._MANIFEST
            return (
                path.parent == self.root
                and path.is_dir()
                and not path.is_symlink()
                and manifest.is_file()
                and not manifest.is_symlink()
            )
        except OSError:
            return False

    def _remove_tree(self, path: Path) -> None:
        if path.is_symlink():
            path.unlink(missing_ok=True)
            return
        if not path.is_dir():
            path.unlink(missing_ok=True)
            return
        for entry in os.scandir(path):
            child = Path(entry.path)
            if entry.is_symlink():
                child.unlink(missing_ok=True)
            elif entry.is_dir(follow_symlinks=False):
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        path.rmdir()

    def _log_failure(self, path: Path, exc: BaseException) -> None:
        log_path = self.root / self._FAILURES
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(f"{time.time():.6f}\t{path.name}\t{type(exc).__name__}\t{exc}\n")
        except OSError:
            pass

    def _cleanup(self, workspace: Workspace) -> None:
        try:
            if workspace.path.parent != self.root or workspace.path.is_symlink():
                raise ServiceError("WORKSPACE_INVALID", 500)
            # Preserve manifest and lock until all payload directories are gone.
            for child in workspace.path.iterdir():
                if child.name not in {self._MANIFEST, self._LOCK}:
                    self._remove_tree(child)
            for child in (workspace.path / self._MANIFEST, workspace.path / self._LOCK):
                if child.is_symlink():
                    child.unlink(missing_ok=True)
                else:
                    child.unlink(missing_ok=True)
            workspace.path.rmdir()
        except Exception as exc:
            self._log_failure(workspace.path, exc)
            raise ServiceError("WORKSPACE_CLEANUP_FAILED", 500) from exc
        finally:
            try:
                fcntl.flock(workspace._lock_file.fileno(), fcntl.LOCK_UN)
                workspace._lock_file.close()
            except (OSError, ValueError):
                pass

    def sweep(self) -> int:
        removed = 0
        now = time.time()
        try:
            entries = list(os.scandir(self.root))
        except OSError as exc:
            raise ServiceError("WORKSPACE_UNAVAILABLE", 503) from exc
        for entry in entries:
            path = Path(entry.path)
            if not entry.is_dir(follow_symlinks=False) or entry.is_symlink() or not self._owned(path):
                continue
            try:
                if re.fullmatch(r"ws-[0-9a-f]{24}", path.name) is None:
                    continue
                manifest_fd = os.open(path / self._MANIFEST, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(manifest_fd, encoding="utf-8") as stream:
                    manifest = json.load(stream)
                if manifest.get("version") != 1:
                    continue
                if now - float(manifest["created_at"]) < self.stale_seconds:
                    continue
                lock = self._open_lock(path / self._LOCK)
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    lock.close()
                    if exc.errno in (errno.EACCES, errno.EAGAIN):
                        continue
                    raise
                try:
                    for child in path.iterdir():
                        if child.name not in {self._MANIFEST, self._LOCK}:
                            self._remove_tree(child)
                    for child in (path / self._MANIFEST, path / self._LOCK):
                        child.unlink(missing_ok=True)
                    path.rmdir()
                    removed += 1
                finally:
                    lock.close()
            except Exception as exc:
                self._log_failure(path, exc)
        return removed

    def cleanup(self, workspace: Workspace) -> None:
        self._cleanup(workspace)
