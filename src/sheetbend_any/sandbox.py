"""Confined, finite execution with a resource-owned staged result."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

import psutil

from ._sandbox.platforms import (
    backend_name, linux_command, macos_profile, require_backend, runtime_paths, worker_environment,
)
from ._version import __version__
from .contracts import canonical_json
from .errors import ExecutionError, SandboxError
from .request import PathLike


@dataclass(frozen=True, slots=True)
class Limits:
    """Hard CPU/per-file/Linux address-space limits plus supervised wall/RSS bounds.

    Linux scratch is a size-limited tmpfs. macOS scratch/RSS checks are polling
    limits, not hard quotas; a run may briefly overshoot them. No child processes
    are permitted on either platform.
    """

    timeout_seconds: float = 30.0
    memory_bytes: int = 512 * 1024 * 1024
    output_bytes: int = 64 * 1024 * 1024
    scratch_bytes: int = 128 * 1024 * 1024
    log_bytes: int = 1024 * 1024
    scratch_files: int = 4096

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive.")
        for name in ("memory_bytes", "output_bytes", "scratch_bytes", "log_bytes", "scratch_files"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class StagedResult:
    """The path exists only inside Sandbox.execute()'s context. Logs are bounded bytes."""

    path: Path
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    backend: str


def _scratch_usage(path: Path, limit: int) -> tuple[int, int]:
    size, count = 0, 0
    pending = [path]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                count += 1
                if count > limit:
                    return size, count
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    size += max(info.st_size, getattr(info, "st_blocks", 0) * 512)
    return size, count


def _supervise(command: list[str], *, env: dict[str, str], cwd: Path, work: Path,
               limits: Limits) -> tuple[bytes, bytes, float]:
    started = time.monotonic()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env, close_fds=True, start_new_session=True,
    )
    observed: dict[int, psutil.Process] = {}
    try:
        with selectors.DefaultSelector() as selector:
            for name in output:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                if time.monotonic() - started > limits.timeout_seconds:
                    raise ExecutionError("Program exceeded its wall-clock time limit.")
                for key, _ in selector.select(timeout=0.025):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(data)
                        if sum(map(len, output.values())) > limits.log_bytes:
                            raise ExecutionError("Program exceeded its diagnostic-output limit.")
                try:
                    root = psutil.Process(process.pid)
                    processes = [root, *root.children(recursive=True)]
                    for item in processes:
                        # psutil process handles retain creation identity for safe kill checks.
                        item.create_time()
                        observed[item.pid] = item
                    rss = sum(item.memory_info().rss for item in processes)
                    if rss > limits.memory_bytes:
                        raise ExecutionError("Program exceeded its resident-memory limit.")
                except psutil.NoSuchProcess:
                    pass
                if sys.platform == "darwin":
                    try:
                        size, count = _scratch_usage(work, limits.scratch_files)
                    except FileNotFoundError:
                        # Worker may remove scratch files during a sample; next sample rechecks.
                        size, count = 0, 0
                    if size > limits.scratch_bytes or count > limits.scratch_files:
                        raise ExecutionError("Program exceeded its supervised scratch limit.")
            returncode = process.wait(timeout=1)
        if returncode:
            detail = bytes(output["stderr"][-4096:]).decode("utf-8", errors="replace")
            message = f"Confined worker exited with status {returncode}. {detail.strip()}"
            if returncode == 125:
                raise SandboxError(message)
            raise ExecutionError(message, returncode=returncode)
        return bytes(output["stdout"]), bytes(output["stderr"]), time.monotonic() - started
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Kill the actual leader too, even if it changed its process group.
            try:
                process.kill()
            except ProcessLookupError:
                pass
        for child in observed.values():
            try:
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    child.kill()
            except psutil.NoSuchProcess:
                pass
        process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()



class Sandbox:
    """OS-backed Python-only execution. There is deliberately no unsafe backend."""

    def __init__(self, *, limits: Limits | None = None, temporary_directory: PathLike | None = None) -> None:
        self.limits = limits if limits is not None else Limits()
        self.temporary_directory = None if temporary_directory is None else Path(temporary_directory).expanduser().resolve(strict=True)

    def fingerprint(self) -> str:
        """Qualification identity, not proof of bitwise runtime reproducibility."""
        executable = Path(sys.executable).resolve()
        info = executable.stat()
        roots, _ = runtime_paths()
        data = {
            "any": __version__, "backend": backend_name(), "python": sys.version,
            "executable": str(executable), "executable_identity": [info.st_size, info.st_mtime_ns],
            "kernel": os.uname().release, "limits": asdict(self.limits),
            "runtime_roots": [[str(path), path.stat().st_mtime_ns] for path in roots],
        }
        return sha256(canonical_json(data)).hexdigest()

    def require_available(self) -> None:
        """Check adapter presence offline; actual policy is checked on every execution."""
        require_backend()

    @contextmanager
    def execute(
        self, code: str, *, inputs: Sequence[Path], parameters: Mapping[str, Any],
        context: Mapping[str, Any], output_suffix: str = ".bin",
    ) -> Iterator[StagedResult]:
        """Run code inside the OS boundary and retain its staged file until context exit."""
        executable = require_backend()
        if not output_suffix.startswith(".") or any(char in output_suffix for char in "/\\\x00"):
            raise ValueError("output_suffix must be a filename suffix, not a path.")
        with tempfile.TemporaryDirectory(prefix="any-", dir=self.temporary_directory) as temporary:
            root = Path(temporary).resolve()
            for path in inputs:
                if path.is_dir() and root.is_relative_to(path):
                    raise SandboxError("Input includes the private workspace. Select a narrower input or another --temporary-directory.")
            control, work = root / "control", root / "work"
            control.mkdir(mode=0o700)
            work.mkdir(mode=0o700)
            output = root / ("result" + output_suffix)
            output.touch(mode=0o600)
            readonly = control / "readonly-canary"
            readonly.write_bytes(b"readonly")
            denied = root / "denied-canary"
            denied.write_bytes(b"private")
            shutil.copyfile(Path(__file__).parent / "_sandbox" / "worker.py", control / "worker.py")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix:
                tcp.bind(("127.0.0.1", 0))
                tcp.listen(1)
                unix_path = root / "probe.sock"
                # AF_UNIX paths are small on macOS; fail rather than skip the IPC canary.
                unix.bind(str(unix_path))
                unix.listen(1)
                if sys.platform.startswith("linux"):
                    command, visible_inputs, visible_output, visible_work = linux_command(
                        executable, control=control, work=work, output=output,
                        inputs=list(inputs), scratch_bytes=self.limits.scratch_bytes,
                    )
                    visible_readonly = "/control/readonly-canary"
                else:
                    profile = root / "profile.sb"
                    profile.write_text(macos_profile(control, work, output, list(inputs)), encoding="utf-8")
                    visible_inputs, visible_output, visible_work = list(map(str, inputs)), str(output), str(work)
                    visible_readonly = str(readonly)
                    command = [str(executable), "-f", str(profile), str(Path(sys.executable).resolve()),
                               "-S", "-s", "-P", "-B", str(control / "worker.py"), str(control / "task.json")]
                config = {
                    "code": code, "inputs": visible_inputs, "output": visible_output,
                    "work": visible_work, "parameters": dict(parameters), "context": dict(context),
                    "limits": asdict(self.limits), "readonly_canary": visible_readonly,
                    "denied_canary": str(denied), "unix_canary": str(unix_path),
                    "tcp_port": tcp.getsockname()[1],
                }
                (control / "task.json").write_bytes(canonical_json(config))
                stdout, stderr, elapsed = _supervise(
                    command, env=worker_environment(str(work)), cwd=work, work=work, limits=self.limits,
                )
            info = output.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > self.limits.output_bytes:
                raise ExecutionError("Worker result is not one bounded, singly linked regular file.")
            yield StagedResult(output, stdout, stderr, elapsed, backend_name())

    def doctor(self) -> dict[str, Any]:
        """Run synthetic boundary canaries and an output round-trip; never contact a model."""
        with self.execute(
            "def run(inputs, output, params, context):\n    output.write_bytes(b'any-ok')\n",
            inputs=(), parameters={}, context={"now": "2000-01-01T00:00:00+00:00", "input_names": []},
        ) as result:
            if result.path.read_bytes() != b"any-ok":
                raise SandboxError("Sandbox output round-trip failed.")
        return {"backend": backend_name(), "canaries_passed": True, "runtime_fingerprint": self.fingerprint()}

    def __repr__(self) -> str:
        return f"Sandbox(platform={sys.platform!r}, limits={self.limits!r})"
