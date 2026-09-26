"""Trusted standalone bootstrap, executed without site packages or a user environment.

Never import this as an execution shortcut. The coordinator launches it through
an OS adapter. All canaries run before compiling or executing a recipe.
"""

import ctypes
import errno
import json
import math
import os
from pathlib import Path
import random
import resource
import socket
import sys

# A positive syscall allow-list for one-process standard-library computations.
# Paths are constrained independently by Bubblewrap's read-only mount namespace.
# No socket, clone/fork, exec, ptrace, signals to other processes, mount, IPC,
# io_uring, chmod/chown, timestamp mutation, or hard/symbolic-link creation.
_ALLOWED_SYSCALLS = (
    "read", "write", "readv", "writev", "pread64", "pwrite64", "close", "close_range",
    "open", "openat", "openat2", "creat", "fstat", "stat", "lstat", "newfstatat", "statx",
    "lseek", "access", "faccessat", "faccessat2", "readlink", "readlinkat", "getdents",
    "getdents64", "fcntl", "dup", "dup2", "dup3", "fsync", "fdatasync", "ftruncate",
    "truncate", "mkdir", "mkdirat", "unlink", "unlinkat", "rmdir", "rename", "renameat",
    "renameat2", "getcwd", "chdir", "fchdir", "statfs", "fstatfs", "flock",
    "copy_file_range", "sendfile", "mmap", "mprotect", "munmap", "mremap", "madvise",
    "brk", "futex", "futex_time64", "rt_sigaction", "rt_sigprocmask", "rt_sigreturn",
    "sigaltstack", "clock_gettime", "clock_getres", "clock_nanosleep", "nanosleep",
    "gettimeofday", "time", "getrandom", "getpid", "getppid", "gettid", "getuid",
    "geteuid", "getgid", "getegid", "getgroups", "getresuid", "getresgid", "getrlimit",
    "getrusage", "uname", "sysinfo", "sched_yield", "sched_getaffinity", "restart_syscall",
    "poll", "ppoll", "select", "pselect6", "exit", "exit_group", "umask",
)


def install_seccomp() -> None:
    """Install a native libseccomp filter; any setup failure stops before recipe execution."""
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    context = library.seccomp_init(0x00050000 | errno.EPERM)  # SCMP_ACT_ERRNO(EPERM)
    if not context:
        raise RuntimeError("libseccomp could not create a filter")
    try:
        for name in _ALLOWED_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number >= 0 and library.seccomp_rule_add(context, 0x7FFF0000, number, 0) != 0:
                raise RuntimeError(f"libseccomp could not allow {name}")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("kernel rejected the seccomp filter")
    finally:
        library.seccomp_release(context)


def apply_limits(limits: dict) -> None:
    """Set hard limits before installing the syscall filter."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    cpu = max(1, math.ceil(limits["timeout_seconds"]))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    size = limits["output_bytes"]
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    if sys.platform.startswith("linux"):
        memory = limits["memory_bytes"]
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


def expect_denied(operation, *, absent_ok: bool = False) -> None:
    try:
        operation()
    except OSError as exc:
        accepted = {errno.EPERM, errno.EACCES, errno.EROFS, errno.EXDEV}
        if absent_ok:
            accepted.add(errno.ENOENT)
        if exc.errno in accepted:
            return
        raise RuntimeError(f"boundary probe returned unexpected errno {exc.errno}") from exc
    raise RuntimeError("an operating-system boundary probe unexpectedly succeeded")


def probe_boundary(config: dict) -> None:
    """Controlled canaries, not user data. These are checks, not a security proof."""
    def read_denied():
        with open(config["denied_canary"], "rb") as handle:
            handle.read(1)

    def write_readonly():
        fd = os.open(config["readonly_canary"], os.O_WRONLY)
        os.close(fd)

    expect_denied(read_denied, absent_ok=sys.platform.startswith("linux"))
    expect_denied(write_readonly)
    linked = Path(config["work"]) / "hardlink-canary"
    expect_denied(lambda: os.link(config["readonly_canary"], linked))

    for family, kind in ((socket.AF_INET, socket.SOCK_STREAM),
                         (socket.AF_INET, socket.SOCK_DGRAM),
                         (socket.AF_UNIX, socket.SOCK_STREAM)):
        def connect(family=family, kind=kind):
            with socket.socket(family, kind) as connection:
                if family == socket.AF_UNIX:
                    connection.connect(config["unix_canary"])
                elif kind == socket.SOCK_DGRAM:
                    connection.sendto(b"any-boundary-probe", ("127.0.0.1", config["tcp_port"]))
                else:
                    connection.connect(("127.0.0.1", config["tcp_port"]))
        expect_denied(connect)

    def fork():
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        raise RuntimeError("fork unexpectedly succeeded")
    expect_denied(fork)

    def spawn():
        pid = os.posix_spawn(sys.executable, [sys.executable, "-S", "-c", "pass"], {})
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        raise RuntimeError("posix_spawn unexpectedly succeeded")
    expect_denied(spawn)
    # Prove the intended writable file and scratch location actually work.
    with open(config["output"], "wb"):
        pass
    scratch = Path(config["work"]) / "write-canary"
    scratch.write_bytes(b"ok")
    scratch.unlink()


def main() -> int:
    try:
        with open(sys.argv[1], encoding="utf-8") as handle:
            config = json.load(handle)
        apply_limits(config["limits"])
        os.umask(0o077)
        random.seed(0)
        if sys.platform.startswith("linux"):
            install_seccomp()
        elif sys.platform != "darwin":
            raise RuntimeError("unsupported operating system")
        probe_boundary(config)
    except Exception as exc:
        print(f"Boundary setup failed ({type(exc).__name__}): {str(exc)[:1500]}", file=sys.stderr)
        return 125
    try:
        namespace = {"__name__": "__any_recipe__"}
        exec(compile(config["code"], "<any-recipe>", "exec"), namespace)
        namespace["run"](
            [Path(path) for path in config["inputs"]], Path(config["output"]),
            config["parameters"], config["context"],
        )
    except BaseException as exc:
        # A SystemExit must not accidentally certify an unfinished operation.
        print(f"Program failed ({type(exc).__name__}): {str(exc)[:3000]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
