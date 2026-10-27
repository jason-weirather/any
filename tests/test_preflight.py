"""IO preflight uses real opens and private probes, never the task destination."""

import builtins
import os
from pathlib import Path

import pytest

from sheetbend_any import Request
from sheetbend_any.errors import PermissionError


def test_new_output_probe_leaves_destination_absent_and_cleans_up(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("data\n")
    output = tmp_path / "result.txt"
    request = Request.bind("copy", input_paths=[source], output_path=output)
    report = request.preflight()
    assert report["inputs"][0]["readable"]
    assert report["output_parent_probe"]
    assert not report["task_execution_performed"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.txt"]


def test_overwrite_probe_replaces_only_a_private_existing_file(tmp_path, monkeypatch):
    output = tmp_path / "result.txt"
    output.write_text("keep this")
    request = Request.bind("replace", output_path=output, overwrite=True)
    original = os.replace
    observed = []

    def replacement(source, destination, **kwargs):
        info = os.stat(destination, dir_fd=kwargs["dst_dir_fd"], follow_symlinks=False)
        observed.append((source, destination, info.st_size))
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", replacement)
    request.preflight()
    assert len(observed) == 1 and observed[0][2] == 0
    assert observed[0][0].startswith(".any-preflight-")
    assert observed[0][1].startswith(".any-preflight-")
    assert output.read_text() == "keep this"
    assert list(tmp_path.iterdir()) == [output]


def test_read_failure_is_detected_even_when_stat_succeeds(tmp_path, monkeypatch):
    source = tmp_path / "locked.txt"
    source.write_text("data")
    request = Request.bind("read", input_paths=[source], output_path="-")
    original = os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path) == source:
            raise builtins.PermissionError(13, "Permission denied", str(path))
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PermissionError, match="Cannot read declared file"):
        request.preflight()


def test_output_create_failure_does_not_create_final_file(tmp_path, monkeypatch):
    output = tmp_path / "result.txt"
    request = Request.bind("create", output_path=output)
    original = os.open

    def denied(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.startswith(".any-preflight-"):
            raise builtins.PermissionError(13, "Permission denied", path)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PermissionError, match="Cannot publish"):
        request.preflight()
    assert not list(tmp_path.iterdir())


def test_probe_cleanup_after_link_failure(tmp_path, monkeypatch):
    request = Request.bind("create", output_path=tmp_path / "result.txt")

    def denied(*args, **kwargs):
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(os, "link", denied)
    with pytest.raises(PermissionError, match="Operation not supported"):
        request.preflight()
    assert not list(tmp_path.iterdir())


def test_plan_only_reads_roots_without_output_write_probe(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "example").write_text("data")
    request = Request.bind("list", input_paths=[source], output_path=tmp_path / "result.txt")

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata-only planning must not test output creation")

    monkeypatch.setattr(os, "link", forbidden)
    monkeypatch.setattr(os, "replace", forbidden)
    report = request.preflight(probe_output=False)
    assert report["inputs"][0]["readable"]
    assert not report["directory_descendants_checked"]
    assert not report["output_parent_probe"]
