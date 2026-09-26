"""Bounded, non-executing input inspection. No imports or commands from an input directory."""

import codecs
import csv
import io
import os
from pathlib import Path
import stat
from typing import Any

from .errors import ContractError
from .request import InputBinding, identity


def inspect_inputs(inputs: tuple[InputBinding, ...], *, sample_bytes: int = 4096) -> list[dict[str, Any]]:
    """Read at most sample_bytes per regular file and 32 immediate directory names.

    Samples are untrusted task data, not instructions. Directory sampling is
    explicitly incomplete and is never used to answer the user's operation.
    """
    if not 0 <= sample_bytes <= 16384:
        raise ValueError("sample_bytes must be between 0 and 16384.")
    result = []
    budget = 16384
    for item in inputs:
        item.recheck()
        row: dict[str, Any] = {"name": item.path.name, "kind": item.kind}
        if item.kind == "directory":
            if sample_bytes and budget:
                entries = []
                with os.scandir(item.path) as iterator:
                    for index, entry in enumerate(iterator):
                        size = len(entry.name.encode("utf-8", errors="replace"))
                        if index == 32 or size > budget:
                            break
                        entries.append(entry.name)
                        budget -= size
                row.update(entries=sorted(entries), entries_are_complete=False)
        else:
            row["size_bytes"] = item.identity[2]
            allowance = min(sample_bytes, budget)
            if allowance:
                fd = os.open(item.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or identity(info) != item.identity:
                        raise ContractError(f"Input changed during inspection: {item.path}")
                    sample = handle.read(allowance)
                budget -= len(sample)
                try:
                    text = codecs.getincrementaldecoder("utf-8")().decode(
                        sample, final=len(sample) == item.identity[2]
                    )
                except UnicodeDecodeError:
                    row["preview"] = None
                else:
                    row["preview"] = text if "\x00" not in text else None
                    row["preview_is_complete"] = len(sample) == item.identity[2]
        result.append(row)
    return result


def read_columns(path: Path, delimiter: str) -> list[str]:
    """Parse a bounded header, including quoted fields. Never scan the complete table."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ContractError(f"Expected a regular table: {path}")
        raw = handle.read(65537)
    try:
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(raw[:65536], final=len(raw) <= 65536)
        buffer = io.StringIO(text, newline="")
        columns = next(csv.reader(buffer, delimiter=delimiter, strict=True))
    except (UnicodeError, csv.Error, StopIteration) as exc:
        raise ContractError(f"Cannot read a valid UTF-8 table header: {path}") from exc
    # A header at the byte bound could be truncated without the CSV parser noticing.
    if len(raw) > 65536 and buffer.tell() >= len(text) and not text.endswith(("\n", "\r")):
        raise ContractError("Table header exceeds the 64 KiB inspection bound.")
    if not columns or len(columns) != len(set(columns)) or any(not name for name in columns):
        raise ContractError(f"Table column names must be nonempty and unique: {path}")
    return columns
