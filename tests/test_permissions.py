import io
import os
import stat

import pytest

from sheetbend_any import Request
from sheetbend_any.errors import ContractError, PermissionError
from sheetbend_any.output import publish_output, validate_output


def test_new_output_never_modifies_input(table, tmp_path):
    request = Request.bind("copy", input_paths=[table], output_path=tmp_path / "out.csv")
    original = table.read_bytes()
    staged = tmp_path / "candidate"
    staged.write_bytes(original)
    publish_output(staged, request)
    assert table.read_bytes() == original
    assert request.output_path.read_bytes() == original
    assert not list(tmp_path.glob(".any-*.tmp"))


def test_existing_output_refused_before_work(table, tmp_path):
    out = tmp_path / "exists.csv"
    out.write_text("keep")
    with pytest.raises(PermissionError, match="already exists"):
        Request.bind("overwrite anything needed", input_paths=[table], output_path=out)
    assert out.read_text() == "keep"


def test_overwrite_does_not_authorize_input_modification(table):
    with pytest.raises(PermissionError, match="modify"):
        Request.bind("edit", input_paths=[table], output_path=table, overwrite=True)


def test_exact_modify_stages_a_replacement(table, tmp_path):
    table.chmod(0o640)
    request = Request.bind("edit", input_paths=[table], output_path=table, modify_path=table)
    candidate = tmp_path / "candidate"
    candidate.write_text("cell_id,CD3\n2,22\n")
    publish_output(candidate, request)
    assert table.read_text() == "cell_id,CD3\n2,22\n"
    assert stat.S_IMODE(table.stat().st_mode) == 0o640


def test_unrelated_modify_permission_is_rejected(table, tmp_path):
    with pytest.raises(PermissionError, match="exact"):
        Request.bind("edit", input_paths=[table], output_path=tmp_path / "out", modify_path=table)


def test_output_inside_directory_input_is_rejected(tmp_path):
    with pytest.raises(PermissionError, match="inside"):
        Request.bind("write", input_paths=[tmp_path], output_path=tmp_path / "new")


def test_links_and_special_files_are_rejected(table, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(table)
    with pytest.raises(PermissionError):
        Request.bind("read", input_paths=[link], output_path="-")
    with pytest.raises(PermissionError):
        Request.bind("write", output_path=link, overwrite=True)
    hard = tmp_path / "hard"
    os.link(table, hard)
    with pytest.raises(PermissionError):
        Request.bind("write", input_paths=[table], output_path=hard, overwrite=True)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(PermissionError):
        Request.bind("read", input_paths=[fifo], output_path="-")


def test_missing_parents_do_not_get_created(tmp_path):
    target = tmp_path / "missing" / "out"
    with pytest.raises(ContractError):
        Request.bind("make", output_path=target)
    assert not target.parent.exists()


def test_stale_input_and_output_are_rejected(table, tmp_path):
    request = Request.bind("copy", input_paths=[table], output_path=tmp_path / "out")
    table.write_text("changed")
    with pytest.raises(ContractError, match="changed"):
        request.recheck()
    request = Request.bind("make", output_path=tmp_path / "out")
    request.output_path.write_text("concurrent writer")
    with pytest.raises(PermissionError, match="changed"):
        request.recheck()


def test_no_overwrite_is_race_safe_at_publication(tmp_path, monkeypatch):
    out, candidate = tmp_path / "out", tmp_path / "candidate"
    candidate.write_text("ours")
    request = Request.bind("make", output_path=out)
    link = os.link
    def race(*args, **kwargs):
        out.write_text("theirs")
        return link(*args, **kwargs)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(PermissionError, match="appeared"):
        publish_output(candidate, request)
    assert out.read_text() == "theirs"
    assert not list(tmp_path.glob(".any-*.tmp"))


def test_parent_replacement_is_rejected(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    request = Request.bind("make", output_path=parent / "out")
    parent.rename(tmp_path / "old")
    parent.mkdir()
    with pytest.raises(PermissionError, match="parent"):
        request.recheck()


def test_stdout_is_explicit_and_short_writes_are_handled(tmp_path):
    class ShortStream(io.BytesIO):
        def write(self, data):
            return super().write(data[:2])
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"abcdef")
    stream = ShortStream()
    publish_output(candidate, Request.bind("print", output_path="-"), stdout=stream)
    assert stream.getvalue() == b"abcdef"
    with pytest.raises(PermissionError):
        Request.bind("print", output_path="-", overwrite=True)


@pytest.mark.parametrize("format,text", [("json", '{"x":NaN}'), ("json", '{bad'),
                                        ("csv", 'a,a\n1,2\n'), ("csv", 'a,b\n1\n'),
                                        ("csv", ''), ("text", b'\xff')])
def test_invalid_candidate_is_not_publishable(tmp_path, format, text):
    path = tmp_path / "candidate"
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    with pytest.raises(ContractError):
        validate_output(path, {"format": format, "required_columns": []}, max_bytes=1024)


def test_dotdot_input_is_canonical_before_overlap_checks(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(PermissionError, match="inside"):
        Request.bind("write", input_paths=[child / ".."], output_path=tmp_path / "out")
