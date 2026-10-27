"""Read-only discovery of native runtime dependencies; never execute a task tool.

The macOS system loader has a shared cache. Non-system tools (including Homebrew
jq) additionally need their individual dylibs, not their whole installation/home.
The fixed Apple otool reader inspects installed, trusted runtime binaries only.
"""

import os
from pathlib import Path
import subprocess
import sys

from ..errors import SandboxError
from .platforms import worker_environment


def _system_library(path: str) -> bool:
    return path.startswith(("/usr/lib/", "/System/Library/"))


def _load_commands(text: str) -> tuple[list[str], list[str]]:
    libraries, rpaths = [], []
    command = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            command = line[4:]
        elif line.startswith("name ") and command in {
            "LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
            "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB",
        }:
            libraries.append(line[5:].rsplit(" (offset ", 1)[0])
        elif line.startswith("path ") and command == "LC_RPATH":
            rpaths.append(line[5:].rsplit(" (offset ", 1)[0])
    return libraries, rpaths


def native_runtime_files(executables: tuple[Path, ...]) -> tuple[Path, ...]:
    """Return exact runtime files, including executable symlink targets.

Linux uses the existing explicitly exposed system runtime trees. An unsupported
non-system loader/dependency fails in the sandbox instead of exposing more paths.
On macOS, inspect the installed binaries' load commands with a fixed system reader.
No command supplied by a recipe is executed during dependency discovery.
"""
    files = {path.resolve(strict=True) for path in executables}
    if sys.platform != "darwin":
        return tuple(sorted(files))
    otool = Path("/usr/bin/otool")
    pending = [(path.resolve(), path.resolve().parent, ()) for path in executables]
    checked = set()
    while pending:
        binary, executable_parent, inherited_rpaths = pending.pop()
        if binary in checked or str(binary).startswith(("/bin/", "/usr/bin/")) or _system_library(str(binary)):
            continue
        checked.add(binary)
        if len(checked) > 128:
            raise SandboxError("Native dependency closure exceeds 128 files.")
        if not otool.is_file():
            raise SandboxError("Inspecting non-system macOS tools requires /usr/bin/otool (Apple Command Line Tools).")
        try:
            result = subprocess.run(
                [str(otool), "-l", str(binary)], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=5, close_fds=True,
                cwd="/", env=worker_environment("/", shell=True), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"Cannot inspect native runtime {binary}: {exc}") from exc
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise SandboxError(f"Cannot inspect native runtime dependencies for {binary}.")
        libraries, local_rpaths = _load_commands(result.stdout)

        def expand(value: str) -> str:
            return value.replace("@loader_path", str(binary.parent)).replace("@executable_path", str(executable_parent))

        rpaths = tuple(expand(value) for value in local_rpaths) + inherited_rpaths
        for library in libraries:
            if _system_library(library):
                # Often present only in dyld's system cache, not as a normal file.
                continue
            value = expand(library)
            candidates = [value]
            if value.startswith("@rpath/"):
                candidates = [str(Path(prefix) / value[7:]) for prefix in rpaths]
            chosen = next((Path(value) for value in candidates
                           if os.path.isabs(value) and Path(value).is_file()), None)
            if chosen is None:
                raise SandboxError(f"Cannot resolve native dependency {library!r} for {binary}; no broad runtime grant was added.")
            actual = chosen.resolve(strict=True)
            files.update((chosen, actual))
            pending.append((actual, executable_parent, rpaths))
    return tuple(sorted(files))
