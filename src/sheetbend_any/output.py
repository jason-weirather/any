"""Validate one staged result and publish only the destination authorized by the caller."""

import codecs
from contextlib import contextmanager
from collections.abc import Iterator
import csv
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import stat
from typing import Any, BinaryIO
import uuid

from .errors import ContractError, PermissionError
from .request import Request, identity


def validate_output(path: Path, contract: dict[str, Any], *, max_bytes: int) -> tuple[str, int]:
    """Stream hashes/text/CSV validation; JSON validation is bounded to 8 MiB.

    A valid format is not proof of the requested transformation's correctness.
    CSV validation checks unique columns and consistent row width, not data types.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise ContractError("Expected one bounded, singly linked regular result file.")
        digest, size = sha256(), 0
        decoder = codecs.getincrementaldecoder("utf-8")() if contract["format"] != "bytes" else None
        try:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ContractError("Result exceeds its output-size limit.")
                digest.update(chunk)
                if decoder is not None:
                    decoder.decode(chunk)
            if decoder is not None:
                decoder.decode(b"", final=True)
        except UnicodeError as exc:
            raise ContractError("Text output must be valid UTF-8.") from exc
        handle.seek(0)
        if contract["format"] == "json":
            if size > 8 * 1024 * 1024:
                raise ContractError("JSON output validation currently supports at most 8 MiB.")
            try:
                json.load(handle, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ContractError("Result is not valid finite JSON.") from exc
        elif contract["format"] in {"csv", "tsv"}:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            try:
                reader = csv.reader(text, delimiter="," if contract["format"] == "csv" else "\t", strict=True)
                header = next(reader)
                if not header or len(header) != len(set(header)) or any(not value for value in header):
                    raise ContractError("Output table columns must be nonempty and unique.")
                if not set(contract["required_columns"]).issubset(header):
                    raise ContractError("Output is missing declared required columns.")
                for index, row in enumerate(reader, start=2):
                    if len(row) != len(header):
                        raise ContractError(f"Output table row {index} has the wrong number of fields.")
            except (csv.Error, StopIteration, UnicodeError) as exc:
                raise ContractError("Result is not a well-formed UTF-8 table.") from exc
            finally:
                text.detach()
    return digest.hexdigest(), size


@contextmanager
def _candidate(path: Path) -> Iterator[BinaryIO]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContractError("Publication source must be a singly linked regular file.")
        yield source


def _write_all(stream: BinaryIO, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = stream.write(view)
        if written is None or written <= 0:
            raise OSError("Output stream did not accept bytes.")
        view = view[written:]


def publish_output(path: Path, request: Request, *, stdout: BinaryIO | None = None) -> None:
    """Publish after validation. No-overwrite uses a link operation that cannot clobber.

    Replacement is atomic for one file but not a compare-and-swap against other
    host writers. Callers must not concurrently modify bound files/directories.
    Existing ordinary permission bits are preserved; ACLs/xattrs are not copied.
    Temporary publication files live in the authorized destination's parent and
    are removed after success/failure. The worker never sees that directory.
    """
    request.recheck()
    if request.output_path is None:
        if stdout is None:
            raise ValueError("A binary stdout stream is required for a stdout request.")
        with _candidate(path) as source:
            while chunk := source.read(1024 * 1024):
                _write_all(stdout, chunk)
        stdout.flush()
        return
    if stdout is not None:
        raise ValueError("stdout must be omitted for a filesystem output.")
    destination = request.output_path
    parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_name = ".any-" + uuid.uuid4().hex + ".tmp"
    created = False
    try:
        parent = os.fstat(parent_fd)
        if (parent.st_dev, parent.st_ino) != request.parent_identity:
            raise PermissionError("Output parent changed before publication.")
        fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        created = True
        with os.fdopen(fd, "wb") as target, _candidate(path) as source:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
            target.flush()
            os.fchmod(target.fileno(), request.output_mode)
            os.fsync(target.fileno())
        try:
            info = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise PermissionError("Output became a non-regular file before publication.")
        if (None if info is None else identity(info)) != request.output_identity:
            raise PermissionError("Output changed while the candidate was being prepared.")
        if request.overwrite or request.modify_path is not None:
            os.replace(temporary_name, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            created = False
        else:
            try:
                os.link(temporary_name, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            except FileExistsError as exc:
                raise PermissionError("Output appeared during publication; it was not overwritten.") from exc
            os.unlink(temporary_name, dir_fd=parent_fd)
            created = False
    finally:
        try:
            if created:
                os.unlink(temporary_name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
