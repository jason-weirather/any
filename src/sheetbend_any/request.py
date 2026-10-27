"""Bind caller-supplied paths and authority before inspecting data or generating code."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import secrets
import stat
from types import MappingProxyType
from typing import Any

from .contracts import canonical_json
from .errors import ContractError, PermissionError

PathLike = str | os.PathLike[str]
Identity = tuple[int, int, int, int, int]


def identity(info: os.stat_result) -> Identity:
    """Metadata observation, not a content hash. Access time deliberately does not participate."""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def named_path(value: PathLike) -> Path:
    """Resolve existing parents once; never follow a final-component symlink."""
    path = Path(value).expanduser().absolute()
    try:
        return path.resolve(strict=True) if path.name == ".." else path.parent.resolve(strict=True) / path.name
    except OSError as exc:
        raise ContractError(f"Parent directory is unavailable: {path.parent}") from exc


def regular_info(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"Expected a regular file, not a link, directory, or device: {path}")
    return info


@dataclass(frozen=True, slots=True)
class InputBinding:
    """One read grant and its initial metadata; directories are not recursive snapshots."""

    path: Path
    kind: str
    identity: Identity

    def recheck(self) -> None:
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise ContractError(f"Input disappeared: {self.path}") from exc
        kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else None
        if kind != self.kind or identity(info) != self.identity:
            raise ContractError(f"Input changed after binding: {self.path}")

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "kind": self.kind, "identity": list(self.identity)}


@dataclass(frozen=True, slots=True)
class Request:
    """A bound invocation. Construct with bind(); task text grants no authority.

    Original inputs remain caller-owned and must remain stable throughout a run.
    One regular-file output or stdout is supported. Input-directory descendants
    cannot be output destinations in this first release.
    """

    task: str | None
    inputs: tuple[InputBinding, ...]
    output_path: Path | None
    overwrite: bool
    modify_path: Path | None
    as_of: str
    parameters: Mapping[str, Any]
    output_identity: Identity | None
    output_mode: int
    parent_identity: tuple[int, int] | None

    @classmethod
    def bind(
        cls, task: str | None, *, input_paths: Sequence[PathLike] = (),
        output_path: PathLike, overwrite: bool = False, modify_path: PathLike | None = None,
        parameters: Mapping[str, Any] | None = None, as_of: str | None = None,
    ) -> "Request":
        """Check filesystem effects early. '-' selects managed stdout, never shell redirection."""
        if task is not None and (not isinstance(task, str) or not task.strip() or len(task) > 16000):
            raise ContractError("Task must be nonempty text of at most 16,000 characters.")
        if isinstance(input_paths, (str, os.PathLike)):
            raise TypeError("input_paths must be an ordered sequence of paths.")
        if len(input_paths) > 32:
            raise ContractError("At most 32 input paths are supported.")
        bound = []
        for value in input_paths:
            path = named_path(value)
            try:
                info = path.lstat()
            except OSError as exc:
                raise ContractError(f"Input is unavailable: {path}") from exc
            kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else None
            if kind is None:
                raise PermissionError(f"Input must be a regular file or directory, not a link/device: {path}")
            if path == Path(path.anchor):
                raise PermissionError("A filesystem root is too broad an input grant; select a directory.")
            bound.append(InputBinding(path, kind, identity(info)))
        output = None if os.fspath(output_path) == "-" else named_path(output_path)
        modify = None if modify_path is None else named_path(modify_path)
        if type(overwrite) is not bool:
            raise TypeError("overwrite must be boolean.")
        if output is None and (overwrite or modify is not None):
            raise PermissionError("Overwrite/modification permission has no meaning for stdout.")
        if modify is not None and (
            output != modify or not any(item.path == modify and item.kind == "file" for item in bound)
        ):
            raise PermissionError("--modify must name the exact file supplied as both input and output.")
        info = None
        parent = None
        if output is not None:
            for item in bound:
                if item.kind == "directory" and output.is_relative_to(item.path):
                    raise PermissionError("Output inside an input directory is not supported; choose a separate destination.")
            info = regular_info(output)
            if info is not None:
                aliases = [item for item in bound if item.identity[:2] == identity(info)[:2]]
                if aliases and modify != output:
                    raise PermissionError(f"Output aliases an input; explicit --modify is required: {output}")
                if info.st_nlink != 1:
                    raise PermissionError(f"Refusing replacement of a multiply linked output: {output}")
                if not overwrite and modify is None:
                    raise PermissionError(f"Output already exists: {output}. Use --overwrite to permit replacement.")
            pinfo = output.parent.stat()
            if not stat.S_ISDIR(pinfo.st_mode):
                raise PermissionError(f"Output parent is not a directory: {output.parent}")
            parent = pinfo.st_dev, pinfo.st_ino
        if as_of is None:
            timestamp = datetime.now().astimezone().isoformat()
        else:
            try:
                parsed = datetime.fromisoformat(as_of)
            except (ValueError, TypeError) as exc:
                raise ContractError("as_of must be an ISO-8601 timestamp with a timezone offset.") from exc
            if parsed.utcoffset() is None:
                raise ContractError("as_of requires an explicit timezone offset.")
            timestamp = parsed.isoformat()
        params = dict(parameters or {})
        canonical_json(params)
        if any(not isinstance(key, str) or isinstance(value, (dict, list)) for key, value in params.items()):
            raise ContractError("Parameters must map names to JSON scalar values.")
        return cls(
            task, tuple(bound), output, overwrite, modify, timestamp, MappingProxyType(params),
            None if info is None else identity(info),
            0o600 if info is None else stat.S_IMODE(info.st_mode) & 0o777,
            parent,
        )

    def recheck(self) -> None:
        """Detect stale direct inputs and destinations, without claiming a filesystem snapshot."""
        for item in self.inputs:
            item.recheck()
        if self.output_path is not None:
            parent = self.output_path.parent.stat()
            if (parent.st_dev, parent.st_ino) != self.parent_identity:
                raise PermissionError("Output parent changed after binding.")
            info = regular_info(self.output_path)
            observed = None if info is None else identity(info)
            if observed != self.output_identity:
                raise PermissionError(f"Output changed after binding: {self.output_path}")

    def context(self) -> dict[str, Any]:
        """Values available to a program; paths and permissions remain separate."""
        return {"now": self.as_of, "input_names": [item.path.name for item in self.inputs]}

    def preflight(self, *, probe_output: bool = True) -> dict[str, Any]:
        """Open declared roots and optionally test publication in the authorized parent.

        The private create/link/replace probes never open the final destination for
        writing. They establish current access, not future quota/space or a snapshot
        of directory descendants. Runtime and publication still recheck authority.
        """
        self.recheck()
        checked = []
        for item in self.inputs:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if item.kind == "directory":
                flags |= os.O_DIRECTORY
            try:
                fd = os.open(item.path, flags)
                try:
                    if identity(os.fstat(fd)) != item.identity:
                        raise ContractError(f"Input changed while opening it: {item.path}")
                    if item.kind == "file":
                        os.read(fd, 1)
                    else:
                        with os.scandir(fd) as entries:
                            next(entries, None)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise PermissionError(f"Cannot read declared {item.kind}: {item.path}: {exc.strerror}") from exc
            checked.append({"path": str(item.path), "kind": item.kind, "readable": True})
        output_checked = False
        if probe_output and self.output_path is not None:
            parent = self.output_path.parent
            fd = None
            names = [f".any-preflight-{secrets.token_hex(12)}" for _ in range(2)]
            created = []
            try:
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != self.parent_identity:
                    raise PermissionError("Output parent changed before permission preflight.")
                handle = os.open(names[0], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                 0o600, dir_fd=fd)
                created.append(names[0])
                try:
                    os.write(handle, b"any-preflight\n")
                finally:
                    os.close(handle)
                if self.output_identity is None:
                    os.link(names[0], names[1], src_dir_fd=fd, dst_dir_fd=fd,
                            follow_symlinks=False)
                    created.append(names[1])
                else:
                    target = os.open(names[1], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                    created.append(names[1])
                    os.close(target)
                    os.replace(names[0], names[1], src_dir_fd=fd, dst_dir_fd=fd)
                output_checked = True
            except OSError as exc:
                raise PermissionError(f"Cannot publish in output directory: {parent}: {exc.strerror}") from exc
            finally:
                if fd is not None:
                    try:
                        for name in created:
                            try:
                                os.unlink(name, dir_fd=fd)
                            except FileNotFoundError:
                                pass
                    finally:
                        os.close(fd)
        self.recheck()
        return {"inputs": checked, "directory_descendants_checked": False,
                "output_parent_probe": output_checked,
                "output_path": None if self.output_path is None else str(self.output_path),
                "task_execution_performed": False}
