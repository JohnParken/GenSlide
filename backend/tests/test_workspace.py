import json
import multiprocessing
import shutil
import time
from pathlib import Path

import pytest

from genslide_agentscope.domain import ServiceError
from genslide_agentscope.workspace import WorkspaceManager


def _sweep_in_child(root: str, result) -> None:
    manager = WorkspaceManager(root, 1_000_000, 0, 0)
    result.put(manager.sweep())


def test_create_and_cleanup_are_bounded(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 60)
    workspace = manager.create("action", "instance")
    assert workspace.path.stat().st_mode & 0o777 == 0o700
    assert all(path.exists() and path.stat().st_mode & 0o777 == 0o700 for path in (workspace.inputs, workspace.scratch, workspace.outputs))
    workspace.cleanup()
    assert not workspace.path.exists()


def test_cleanup_removes_extra_workspace_root_files(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 60)
    workspace = manager.create("action", "instance")
    extra = workspace.path / "runtime-output.tmp"
    extra.write_text("intermediate")
    workspace.cleanup()
    assert not extra.exists()
    assert not workspace.path.exists()


def test_capacity_is_checked(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 0, 0, 60)
    with pytest.raises(ServiceError, match="WORKSPACE_CAPACITY"):
        manager.create("a", "i")


def test_check_usage_rechecks_actual_bytes(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 60)
    workspace = manager.create("a", "i")
    (workspace.outputs / "large").write_bytes(b"x" * 32)
    manager.max_bytes = 1
    with pytest.raises(ServiceError, match="WORKSPACE_CAPACITY"):
        workspace.check_usage()
    workspace._lock_file.close()


def test_check_usage_rejects_insufficient_disk(tmp_path: Path, monkeypatch):
    manager = WorkspaceManager(tmp_path, 1_000_000, 100, 60)
    usage_type = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage_type(0, 0, 50))
    with pytest.raises(ServiceError, match="WORKSPACE_CAPACITY"):
        manager.check_usage()


def test_root_symlink_is_rejected_without_chmodifying_target(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    link = tmp_path / "root-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ServiceError, match="WORKSPACE_INVALID"):
        WorkspaceManager(link, 1000, 0, 60)
    assert target.stat().st_mode & 0o777 == 0o755


def test_sweep_removes_stale_but_not_active(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 1)
    active = manager.create("active", "i")
    stale = manager.create("stale", "i")
    manifest = stale.path / manager._MANIFEST
    data = json.loads(manifest.read_text())
    data["created_at"] = time.time() - 10
    manifest.write_text(json.dumps(data))
    stale._lock_file.close()
    assert manager.sweep() == 1
    assert active.path.exists()
    active.cleanup()


def test_sweep_skips_active_lock_in_another_process(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 0)
    workspace = manager.create("active", "i")
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(target=_sweep_in_child, args=(str(tmp_path), result))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    assert result.get(timeout=2) == 0
    assert workspace.path.exists()
    workspace.cleanup()


def test_sweep_ignores_symlinked_workspace(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 0)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep").write_text("safe")
    link = tmp_path / "ws-link"
    link.symlink_to(target, target_is_directory=True)
    assert manager.sweep() == 0
    assert (target / "keep").exists()


def test_sweep_rejects_symlink_manifest_and_lock(tmp_path: Path):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 0)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    manifest_workspace = manager.create("manifest", "i")
    manifest_workspace._lock_file.close()
    (manifest_workspace.path / manager._MANIFEST).unlink()
    (manifest_workspace.path / manager._MANIFEST).symlink_to(outside / "keep")
    lock_workspace = manager.create("lock", "i")
    lock_workspace._lock_file.close()
    (lock_workspace.path / manager._LOCK).unlink()
    (lock_workspace.path / manager._LOCK).symlink_to(outside / "keep")
    assert manager.sweep() == 0
    assert outside.joinpath("keep").exists()


def test_sweep_leaves_unowned_directories_and_recovers_failed_cleanup(tmp_path: Path, monkeypatch):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 0)
    stranger = tmp_path / "ws-not-a-random-name"
    stranger.mkdir()
    (stranger / "keep").write_text("safe")
    workspace = manager.create("a", "i")
    (workspace.outputs / "nested").mkdir()
    (workspace.outputs / "nested" / "value").write_text("x")
    original = shutil.rmtree
    monkeypatch.setattr("genslide_agentscope.workspace.shutil.rmtree", lambda path: (_ for _ in ()).throw(OSError("transient")))
    with pytest.raises(ServiceError, match="WORKSPACE_CLEANUP_FAILED"):
        workspace.cleanup()
    monkeypatch.setattr("genslide_agentscope.workspace.shutil.rmtree", original)
    assert manager.sweep() == 1
    assert stranger.joinpath("keep").exists()


def test_cleanup_failure_preserves_manifest_and_can_retry(tmp_path: Path, monkeypatch):
    manager = WorkspaceManager(tmp_path, 1_000_000, 0, 60)
    workspace = manager.create("a", "i")
    (workspace.outputs / "nested").mkdir()
    (workspace.outputs / "nested" / "value").write_text("x")
    original = __import__("shutil").rmtree
    calls = {"count": 0}

    def fail_once(path):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("transient")
        return original(path)

    monkeypatch.setattr("genslide_agentscope.workspace.shutil.rmtree", fail_once)
    with pytest.raises(ServiceError, match="WORKSPACE_CLEANUP_FAILED"):
        workspace.cleanup()
    assert (workspace.path / manager._MANIFEST).exists()
    workspace.cleanup()
    assert not workspace.path.exists()
