"""Confined, finite execution with a resource-owned staged result."""

from collections.abc import Callable, Iterator, Mapping, Sequence
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
from ._sandbox.native import native_runtime_files
from ._version import __version__
from .contracts import canonical_json
from .errors import ContractError, ExecutionError, SandboxError
from .request import PathLike


@dataclass(frozen=True, slots=True)
class Limits:
    """Hard CPU/per-file/Linux address-space limits plus supervised wall/RSS bounds.

    Linux scratch is a size-limited tmpfs. macOS scratch/RSS checks are polling
    limits, not hard quotas; a run may briefly overshoot them. Python recipes have
    no child processes; shell jobs permit confined children with a supervised cap.
    """

    timeout_seconds: float = 30.0
    memory_bytes: int = 512 * 1024 * 1024
    output_bytes: int = 64 * 1024 * 1024
    scratch_bytes: int = 128 * 1024 * 1024
    log_bytes: int = 1024 * 1024
    scratch_files: int = 4096
    processes: int = 64

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive.")
        for name in ("memory_bytes", "output_bytes", "scratch_bytes", "log_bytes", "scratch_files", "processes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class StagedResult:
    """A private snapshot retained for the execution context; logs are bounded bytes."""

    path: Path
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    backend: str
    command: str | None = None
    tools: dict[str, Any] | None = None


def _snapshot_output(source: Path, destination: Path, *, max_bytes: int) -> None:
    """Freeze task bytes in a new inode the confined process was never granted.

    In particular, a missed macOS descendant retaining the worker's output FD
    cannot change the later validation/publication source. Concurrent changes to
    the original may affect the copied bytes, which still require normal output
    validation; they cannot affect the completed snapshot. The copy is bounded
    independently of the worker-reported length and all bytes remain untrusted.
    """
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(source_fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise ExecutionError("Worker result is not one bounded, singly linked regular file.")
        target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(target_fd, "wb") as target:
            copied = 0
            while chunk := handle.read(min(1024 * 1024, max_bytes - copied + 1)):
                copied += len(chunk)
                if copied > max_bytes:
                    raise ExecutionError("Worker result exceeded its limit while being snapshotted.")
                target.write(chunk)
            after = os.fstat(handle.fileno())
            if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                    or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
                raise ExecutionError("Worker result changed while being snapshotted.")


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
                    if len(processes) > limits.processes:
                        raise ExecutionError("Program exceeded its supervised process-count limit.")
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
            description = f"status {returncode}"
            if returncode < 0:
                try:
                    description += f" ({signal.Signals(-returncode).name})"
                except ValueError:
                    description += " (unknown signal)"
            message = f"Confined worker exited with {description}. {detail.strip()}"
            if returncode == 125:
                raise SandboxError(message)
            raise ExecutionError(message, returncode=returncode)
        return bytes(output["stdout"]), bytes(output["stderr"]), time.monotonic() - started
    finally:
        # A successful leader can still leave a child holding a pipe or writing
        # scratch. Tear down the original group even after the leader exits.
        # On macOS an unobserved child can escape this group between polls;
        # callers therefore receive an ungranted snapshot, never the writable
        # worker inode. This supervisor is not a complete macOS daemon tracker.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
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
        deadline = time.monotonic() + 2
        while True:
            alive = []
            for child in observed.values():
                try:
                    if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                        alive.append(child)
                except psutil.NoSuchProcess:
                    pass
            if not alive:
                break
            if time.monotonic() >= deadline:
                raise ExecutionError("Confined descendants did not stop; no staged result was released.")
            time.sleep(0.01)



class Sandbox:
    """OS-backed Python recipes and native pipelines; no unsafe backend."""

    def __init__(
        self, *, limits: Limits | None = None, temporary_directory: PathLike | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None, verbose: int = 0,
    ) -> None:
        self.limits = limits if limits is not None else Limits()
        self.temporary_directory = None if temporary_directory is None else Path(temporary_directory).expanduser().resolve(strict=True)
        self.on_event = on_event
        self.verbose = verbose

    def _event(self, event: str, *, level: int = 2, **detail: Any) -> None:
        if self.verbose < level:
            return
        if self.on_event is not None:
            self.on_event({"event": event, "level": level, **detail})
        elif self.verbose >= level:
            import json
            print(json.dumps({"event": event, **detail}, ensure_ascii=True)[:16384], file=sys.stderr)

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

    def require_shell_available(self) -> None:
        """Presence check only. preflight_shell checks the actual OS boundary."""
        from .command_plan import discover_commands
        require_backend()
        if not discover_commands(["bash"])["bash"]["available"]:
            raise SandboxError("Shell execution requires an installed Bash runtime.")

    @contextmanager
    def execute(
        self, code: str, *, inputs: Sequence[Path], parameters: Mapping[str, Any],
        context: Mapping[str, Any], output_suffix: str = ".bin",
    ) -> Iterator[StagedResult]:
        """Run code inside the OS boundary and retain its staged file until context exit."""
        with self._execute(code, inputs=inputs, parameters=parameters, context=context,
                           output_suffix=output_suffix) as result:
            yield result

    @contextmanager
    def execute_shell(
        self, pipeline: list[dict[str, Any]], *, inputs: Sequence[Path],
        parameters: Mapping[str, Any], context: Mapping[str, Any],
        output_suffix: str = ".bin", preflight: bool = False,
        inventory: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Iterator[StagedResult]:
        """Run the rendered Bash pipeline with inherited filesystem/network confinement.

        preflight runs only trusted boundary/readability probes and a fixed Bash
        no-op; it never runs the supplied task pipeline or publishes its output.
        """
        with self._execute(None, inputs=inputs, parameters=parameters, context=context,
                           output_suffix=output_suffix, pipeline=pipeline, preflight=preflight,
                           inventory=inventory) as result:
            yield result

    def preflight_shell(
        self, pipeline: list[dict[str, Any]], *, inputs: Sequence[Path],
        parameters: Mapping[str, Any], context: Mapping[str, Any],
        inventory: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.execute_shell(pipeline, inputs=inputs, parameters=parameters,
                                context=context, preflight=True, inventory=inventory) as result:
            return {"backend": result.backend, "canaries_passed": True,
                    "task_executed": False, "command": result.command, "tools": result.tools}

    @contextmanager
    def _execute(
        self, code: str | None, *, inputs: Sequence[Path], parameters: Mapping[str, Any],
        context: Mapping[str, Any], output_suffix: str = ".bin",
        pipeline: list[dict[str, Any]] | None = None, preflight: bool = False,
        inventory: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Iterator[StagedResult]:
        executable = require_backend()
        shell = pipeline is not None
        script = None
        shell_executables: tuple[Path, ...] = ()
        runtime_files: tuple[Path, ...] = ()
        if shell:
            from .command_plan import discover_commands, render_pipeline, resolve_pipeline, validate_pipeline
            validate_pipeline(pipeline, len(inputs), parameters)
            discovered = discover_commands(["bash", *(stage["argv"][0] for stage in pipeline)])
            if inventory is None:
                inventory = discovered
            else:
                frozen = {}
                for name, current in discovered.items():
                    expected = inventory.get(name, {})
                    if not current["available"] or expected.get("path") != current["path"]:
                        raise ContractError(f"Selected executable {name!r} changed or is unavailable; prepare a new plan.")
                    info = Path(current["path"]).stat()
                    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                    if expected.get("identity") != identity:
                        raise ContractError(f"Selected executable {name!r} changed identity; prepare a new plan.")
                    frozen[name] = dict(expected)
                inventory = frozen
            if not inventory["bash"]["available"]:
                raise SandboxError("Shell execution requires an installed Bash runtime.")
            # Validate availability/options before allocating a private workspace.
            resolve_pipeline(pipeline, inputs, parameters, context, inventory)
            shell_executables = tuple(Path(item["path"]) for item in inventory.values())
            runtime_files = native_runtime_files(shell_executables)
            for item in inventory.values():
                info = Path(item["path"]).stat()
                item["identity"] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        if not output_suffix.startswith(".") or any(char in output_suffix for char in "/\\\x00"):
            raise ValueError("output_suffix must be a filename suffix, not a path.")
        with tempfile.TemporaryDirectory(prefix="any-", dir=self.temporary_directory) as temporary:
            root = Path(temporary).resolve()
            runtime_roots, _ = runtime_paths()
            if any(root.is_relative_to(path.resolve()) for path in runtime_roots):
                raise SandboxError("Private workspace overlaps a runtime read grant. Select another --temporary-directory.")
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
                        shell_executables=shell_executables, runtime_files=runtime_files,
                    )
                    visible_readonly = "/control/readonly-canary"
                else:
                    profile = root / "profile.sb"
                    policy = macos_profile(control, work, output, list(inputs),
                                           shell_executables=shell_executables, runtime_files=runtime_files)
                    profile.write_text(policy, encoding="utf-8")
                    self._event("sandbox.policy", backend=backend_name(), policy=policy[:16384])
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
                if shell:
                    bound = resolve_pipeline(pipeline, visible_inputs, parameters, context, inventory)
                    script = render_pipeline(bound)
                    config.update({"mode": "shell", "script": script,
                                   "bash": inventory["bash"]["path"], "preflight": preflight,
                                   "executables": [str(path) for path in shell_executables]})
                    self._event("command", level=1, script=script,
                                phase=context.get("any_phase", "run"),
                                inputs=[{"host": str(host), "sandbox": visible}
                                        for host, visible in zip(inputs, visible_inputs, strict=True)],
                                staged_output=visible_output, preflight=preflight)
                (control / "task.json").write_bytes(canonical_json(config))
                self._event("sandbox.launch", backend=backend_name(), argv=command,
                            limits=asdict(self.limits), mode="shell" if shell else "python")
                stdout, stderr, elapsed = _supervise(
                    command, env=worker_environment(str(work), shell=shell), cwd=work, work=work, limits=self.limits,
                )
                if stderr:
                    self._event("sandbox.diagnostics", stderr=stderr[-8192:].decode("utf-8", errors="replace"))
            # This path is a sibling of the granted output, outside /work and
            # every write grant. Only this stable copy is exposed to validators.
            candidate = root / ("candidate" + output_suffix)
            _snapshot_output(output, candidate, max_bytes=self.limits.output_bytes)
            yield StagedResult(candidate, stdout, stderr, elapsed, backend_name(), script, inventory)

    def doctor(self) -> dict[str, Any]:
        """Run synthetic boundary canaries and an output round-trip; never contact a model."""
        with self.execute(
            "def run(inputs, output, params, context):\n    output.write_bytes(b'any-ok')\n",
            inputs=(), parameters={}, context={"now": "2000-01-01T00:00:00+00:00", "input_names": []},
        ) as result:
            if result.path.read_bytes() != b"any-ok":
                raise SandboxError("Sandbox output round-trip failed.")
        with tempfile.TemporaryDirectory(prefix="any-doctor-", dir=self.temporary_directory) as temporary:
            source = Path(temporary) / "input.txt"
            source.write_bytes(b"any-doctor\n")
            pipeline = [{"argv": ["cat", {"input": 0}]}, {"argv": ["wc", "-l"]}]
            with self.execute_shell(pipeline, inputs=[source], parameters={}, context={}) as result:
                if result.path.read_bytes().strip() != b"1":
                    raise SandboxError("Native shell pipeline round-trip failed.")
        return {"backend": backend_name(), "canaries_passed": True,
                "python_canaries_passed": True, "shell_canaries_passed": True,
                "runtime_fingerprint": self.fingerprint()}

    def __repr__(self) -> str:
        return f"Sandbox(platform={sys.platform!r}, limits={self.limits!r})"
