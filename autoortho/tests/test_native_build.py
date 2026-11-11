"""Checks for release provenance and bounded diagnostic retention."""

import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_build as nb
import windows_crash as wc


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    for name in nb.WINDOWS_FILES:
        (tmp_path / name).write_bytes(name.encode())
    monkeypatch.setattr(nb, "source_hashes", lambda root, platform_name="windows": {"source.c": "source-hash"})
    nb.record_bundle(tmp_path, tmp_path, "test-revision")
    return tmp_path


def test_native_bundle_verifies_exact_files_and_sources(bundle):
    assert nb.verify_bundle(bundle, bundle)["revision"] == "test-revision"


def test_old_or_modified_dll_is_rejected(bundle):
    (bundle / "aopipeline.dll").write_bytes(b"old bundled DLL")
    with pytest.raises(ValueError, match="aopipeline.dll"):
        nb.verify_bundle(bundle)


def test_binaries_from_other_sources_are_rejected(bundle, monkeypatch):
    monkeypatch.setattr(nb, "source_hashes", lambda root, platform_name="windows": {"source.c": "different"})
    with pytest.raises(ValueError, match="different sources"):
        nb.verify_bundle(bundle, bundle)


def test_manifest_cannot_select_paths_outside_bundle(bundle):
    path = bundle / nb.MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["files"]["../other.dll"] = "ignored"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Invalid"):
        nb.verify_bundle(bundle)


def test_retention_preserves_active_collectors_and_other_files(tmp_path):
    active = tmp_path / "worker-active.dmp"
    active.write_bytes(b"active")
    other = tmp_path / "unrelated.txt"
    other.write_text("keep")
    for index in range(5):
        prefix = "worker-" + str(index)
        for suffix in (".complete", ".dmp", ".crash.json", ".build.json", ".modules.jsonl"):
            path = tmp_path / (prefix + suffix)
            path.touch()
            os.utime(path, (index + 1, index + 1))
    wc.prune_reports(tmp_path)
    assert len(list(tmp_path.glob("*.complete"))) == 3
    assert not (tmp_path / "worker-0.dmp").exists()
    assert active.exists() and other.exists()


def test_collector_spawn_failure_releases_startup_gate(bundle, monkeypatch):
    class Kernel:
        def __init__(self):
            self.signals = []
        def CreateEventW(self, *args):
            return 123
        def SetEvent(self, event):
            self.signals.append(event)
        def CloseHandle(self, event):
            pass
    kernel = Kernel()
    monkeypatch.setattr(wc, "_kernel32", lambda: kernel)
    monkeypatch.setattr(wc, "native_directory", lambda: bundle)
    def fail(*args, **kwargs):
        raise OSError("cannot start collector")
    monkeypatch.setattr(wc.subprocess, "Popen", fail)
    collector = wc.WindowsCrashCollector(bundle / "reports")
    collector.start(456, None)
    assert kernel.signals == [123]
    collector.close()


def test_frozen_worker_refuses_mismatched_native_bundle(bundle, monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "win32")
    monkeypatch.setattr(wc.sys, "frozen", True, raising=False)
    monkeypatch.setattr(wc, "native_directory", lambda: bundle)
    (bundle / "aopipeline.dll").write_bytes(b"wrong")
    with pytest.raises(RuntimeError, match="failed verification"):
        wc.check_native_build()


@pytest.mark.parametrize("platform_name", ["linux", "macos"])
def test_posix_bundle_rejects_stale_staged_library(tmp_path, monkeypatch, platform_name):
    monkeypatch.setattr(nb, "source_hashes", lambda root, platform_name="windows": {"pipeline.c": "current-source"})
    name = nb.PLATFORM_FILES[platform_name][0]
    (tmp_path / name).write_bytes(b"newly compiled pipeline")
    manifest = nb.record_bundle(tmp_path, tmp_path, "revision", platform_name)
    assert nb.verify_bundle(tmp_path, tmp_path, expected_platform=platform_name) == manifest
    with pytest.raises(ValueError, match="wrong platform"):
        nb.verify_bundle(tmp_path, expected_platform="windows")
    (tmp_path / name).write_bytes(b"old checked-in pipeline")
    with pytest.raises(ValueError, match="hash mismatch"):
        nb.verify_bundle(tmp_path, tmp_path)


@pytest.mark.parametrize("platform_name", ["linux", "macos"])
def test_posix_bundle_rejects_different_source(tmp_path, monkeypatch, platform_name):
    name = nb.PLATFORM_FILES[platform_name][0]
    (tmp_path / name).write_bytes(b"native library")
    monkeypatch.setattr(nb, "source_hashes", lambda root, platform_name="windows": {"pipeline.c": "old-source"})
    nb.record_bundle(tmp_path, tmp_path, "revision", platform_name)
    monkeypatch.setattr(nb, "source_hashes", lambda root, platform_name="windows": {"pipeline.c": "new-source"})
    with pytest.raises(ValueError, match="different sources"):
        nb.verify_bundle(tmp_path, tmp_path)


@pytest.mark.parametrize("compiled_version", ["1.1.0", "1.0.0"])
def test_runtime_component_versions_are_checked_against_source(tmp_path, monkeypatch, compiled_version):
    import ctypes
    from types import SimpleNamespace
    source = tmp_path / "autoortho/aopipeline"
    source.mkdir(parents=True)
    functions = {}
    for name in ("aocache", "aodecode", "aodds"):
        (source / (name + ".c")).write_text('#define ' + name.upper() + '_VERSION "1.1.0"\n')
        def version(name=name):
            return (name + " " + compiled_version + " (OpenMP)").encode()
        functions[name + "_version"] = version
    monkeypatch.setattr(ctypes, "CDLL", lambda path: SimpleNamespace(**functions))
    if compiled_version == "1.0.0":
        with pytest.raises(ValueError, match="version mismatch"):
            nb.verify_versions(tmp_path, tmp_path, "linux")
    else:
        assert len(nb.verify_versions(tmp_path, tmp_path, "linux")) == 3
