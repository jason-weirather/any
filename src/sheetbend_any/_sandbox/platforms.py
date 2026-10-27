"""Explicit filesystem layouts and macOS policy. Never mount an entire Python prefix."""

from collections.abc import Sequence
import json
from pathlib import Path
import shutil
import sys
import sysconfig

from ..errors import SandboxError


def runtime_paths() -> tuple[list[Path], list[Path]]:
    """Read-only runtime trees plus denied third-party package locations.

    Runtime installations are trusted dependencies, not user input. Their library
    trees must not contain private application data. Site directories are masked.
    """
    candidates = [Path(sysconfig.get_path("stdlib"))]
    libdir = sysconfig.get_config_var("LIBDIR")
    if libdir:
        candidates.append(Path(libdir))
    candidates.extend(Path(value) for value in (
        ("/usr/lib", "/usr/lib64", "/lib", "/lib64") if sys.platform.startswith("linux") else
        ("/usr/lib", "/System/Library", "/private/var/db/dyld")
    ))
    roots = []
    for path in sorted(set(candidates), key=lambda item: (len(item.parts), str(item))):
        if path.is_dir() and not any(path.is_relative_to(parent) for parent in roots):
            roots.append(path)
    blocked = {Path(sysconfig.get_path(key)) for key in ("purelib", "platlib")}
    blocked.update(Path(value) for value in (
        "/usr/lib/python3/dist-packages",
        str(Path(sysconfig.get_path("stdlib")) / "site-packages"),
    ))
    denied = {path for path in blocked if path.exists()}
    # /lib and /usr/lib may expose the same host tree at different sandbox paths.
    # Mask every mounted spelling, not only sysconfig's preferred spelling.
    for path in list(denied):
        actual = path.resolve()
        for root in roots:
            resolved_root = root.resolve()
            if actual.is_relative_to(resolved_root):
                denied.add(root / actual.relative_to(resolved_root))
    return roots, sorted(denied)


def backend_name() -> str:
    if sys.platform.startswith("linux"):
        return "bubblewrap-seccomp"
    if sys.platform == "darwin":
        return "macos-seatbelt"
    raise SandboxError("Execution supports Linux and macOS only; --plan-only remains available.")


def require_backend() -> Path:
    if sys.platform.startswith("linux"):
        selected = shutil.which("bwrap")
        if selected is None:
            raise SandboxError("Execution requires Bubblewrap (bwrap) and libseccomp on Linux. No unsandboxed fallback. Use --plan-only to preview.")
        return Path(selected).resolve()
    if sys.platform == "darwin":
        selected = Path("/usr/bin/sandbox-exec")
        if selected.is_file():
            return selected
        raise SandboxError("macOS sandbox-exec is unavailable. No unsandboxed fallback. Use --plan-only to preview.")
    raise SandboxError("Execution supports Linux and macOS only; use --plan-only to preview.")


def linux_command(
    executable: Path, *, control: Path, work: Path, output: Path,
    inputs: list[Path], scratch_bytes: int, shell_executables: tuple[Path, ...] = (),
    runtime_files: tuple[Path, ...] = (),
) -> tuple[list[str], list[str], str, str]:
    """Construct a new read-only root, read-only inputs, one writable file, bounded tmpfs."""
    python = Path(sys.executable).resolve()
    command = [str(executable), "--unshare-all", "--die-with-parent", "--new-session",
               "--cap-drop", "ALL", "--clearenv"]
    roots, blocked = runtime_paths()
    for root in roots:
        command.extend(["--ro-bind", str(root), str(root)])
    command.extend(["--ro-bind", str(python), str(python)])
    for path in dict.fromkeys((*shell_executables, *runtime_files)):
        if not any(path.is_relative_to(root) for root in roots):
            command.extend(["--ro-bind", str(path), str(path)])
    for path in blocked:
        if any(path.is_relative_to(root) for root in roots):
            command.extend(["--size", "4096", "--tmpfs", str(path), "--remount-ro", str(path)])
    loader_cache = Path("/etc/ld.so.cache")
    if loader_cache.is_file():
        command.extend(["--ro-bind", str(loader_cache), str(loader_cache)])
    for device in ("/dev/null", "/dev/urandom"):
        command.extend(["--dev-bind", device, device])
    command.extend(["--ro-bind", str(control), "/control", "--dir", "/inputs"])
    visible_inputs = []
    for index, path in enumerate(inputs):
        target = f"/inputs/{index}/{path.name}" if path.is_file() else f"/inputs/{index}"
        command.extend(["--ro-bind", str(path), target])
        visible_inputs.append(target)
    result = f"/result/{output.name}"
    command.extend([
        "--bind", str(output), result,
        "--size", str(scratch_bytes), "--perms", "0700", "--tmpfs", "/work",
        "--chdir", "/work", "--remount-ro", "/",
    ])
    environment = worker_environment("/work", shell=bool(shell_executables))
    for key, value in environment.items():
        command.extend(["--setenv", key, value])
    command.extend(["--", str(python), "-S", "-s", "-P", "-B", "/control/worker.py", "/control/task.json"])
    return command, visible_inputs, result, "/work"


def _filter(path: Path, *, directory: bool) -> str:
    value = str(path.resolve())
    if len(value.encode("utf-8")) > 900 or any(ord(char) < 32 for char in value):
        raise SandboxError("Path cannot be represented safely in the macOS sandbox profile.")
    return f"({'subpath' if directory else 'literal'} {json.dumps(value, ensure_ascii=False)})"


def macos_visible_inputs(inputs: Sequence[Path]) -> list[Path]:
    """Use the *same* canonical input names in Seatbelt and the worker.

    On macOS, /var, /tmp and /etc may be symlink aliases into /private. A
    literal read grant to the resolved vnode is not sufficient if the worker
    opens a different spelling whose symlink traversal is denied. This must
    not add a grant for the enclosing directory or other undeclared files.
    """
    try:
        return [Path(path).resolve(strict=True) for path in inputs]
    except (OSError, RuntimeError) as exc:
        raise SandboxError("Cannot resolve a declared macOS input for the sandbox.") from exc


def _literal_spelling(path: Path) -> str:
    """A metadata-only rule for a symlink's spelling, not its contents."""
    value = str(path.absolute())
    if len(value.encode("utf-8")) > 900 or any(ord(char) < 32 for char in value):
        raise SandboxError("Path cannot be represented safely in the macOS sandbox profile.")
    return f"(literal {json.dumps(value, ensure_ascii=False)})"


def macos_profile(
    control: Path, work: Path, output: Path, inputs: list[Path], *,
    shell_executables: tuple[Path, ...] = (), runtime_files: tuple[Path, ...] = (),
) -> str:
    """Default deny with explicit read/write paths and no network or service IPC.

    Python mode denies forks and permits only its interpreter. Shell mode allows
    confined children and the exact selected executables. Boundary canaries run
    before task code, including inherited denials in a shell-mode child. Missing
    policy support fails execution; it never selects an allow-all mode.
    """
    roots, blocked = runtime_paths()
    python = Path(sys.executable).resolve()
    read = [*(_filter(path, directory=True) for path in roots),
            _filter(python, directory=False), _filter(control, directory=True),
            _filter(work, directory=True), _filter(output, directory=False),
            *(_filter(path, directory=False) for path in (*shell_executables, *runtime_files)),
            *(_filter(path, directory=path.is_dir()) for path in inputs)]
    executables = [python, *shell_executables]
    profile = [
        "(version 1)", "(deny default)",
        # macOS may abort the sandboxed process at launch unless it can read
        # the root directory node. A literal grant does not expose descendants.
        '(allow file-read* (literal "/"))',
        f"(allow process-exec {' '.join(_filter(path, directory=False) for path in executables)})",
        "(allow process-info* (target self))",
        "(allow sysctl-read)",
        f"(allow file-read* {' '.join(read)})",
        f"(allow file-read* file-write* {_filter(work, directory=True)})",
        f"(allow file-write-data {_filter(output, directory=False)})",
        '(allow file-read-data file-write-data file-ioctl (literal "/dev/null"))',
        '(allow file-read-data (literal "/dev/urandom") (literal "/dev/random"))',
    ]
    # Traversing parents needs metadata, not directory listings or file contents.
    ancestors = {parent for path in [*executables, *runtime_files, control, work, output, *inputs, *roots]
                 for parent in (*path.parents, *path.resolve().parents)}
    profile.append(
        f"(allow file-read-metadata file-test-existence "
        f"{' '.join(_filter(path, directory=False) for path in sorted(ancestors))})"
    )
    # These are exact permitted files, not extra directory grants. On some
    # macOS releases access(2) also requires file-test-existence.
    profile.append(
        f"(allow file-test-existence "
        f"{' '.join(_filter(path, directory=path.is_dir()) for path in [*executables, *runtime_files, *inputs])})"
    )
    # Allow only metadata/existence on symlink *nodes* used in traversal.
    # Never add file-read-data or a recursive grant for /var, /tmp, or a tool
    # installation prefix. This also covers Homebrew's executable symlinks.
    symlinks = {
        part for path in [*executables, *runtime_files, control, work, output, *inputs]
        for part in (path, *path.parents) if part.is_symlink()
    }
    if symlinks:
        profile.append(
            "(allow file-read-metadata file-test-existence "
            + " ".join(_literal_spelling(path) for path in sorted(symlinks)) + ")"
        )
    for path in blocked:
        profile.append(f"(deny file-read* {_filter(path, directory=True)})")
    # Do not rely on rule-order subtleties for these absolute denials.
    profile.extend([
        "(allow process-fork)" if shell_executables else "(deny process-fork)",
        "(deny network*)", "(deny system-socket)",
    ])
    return "\n".join(profile) + "\n"


def worker_environment(work: str, *, shell: bool = False) -> dict[str, str]:
    """An allow-list, not a copy of the coordinator's environment."""
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": work, "TMPDIR": work,
        "LANG": "C.UTF-8" if sys.platform.startswith("linux") else "en_US.UTF-8",
        "LC_ALL": "C.UTF-8" if sys.platform.startswith("linux") else "en_US.UTF-8",
        "TZ": "UTC", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1",
    }
    if shell:
        # No BASH_ENV, ENV, shell functions, loader overrides, credentials, or user
        # configuration. A fixed bytewise locale also avoids hidden locale data.
        environment.update({"LANG": "C", "LC_ALL": "C"})
    return environment
