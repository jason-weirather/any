"""Native-process policy components and actual confinement are separate checks."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from sheetbend_any import Limits, Sandbox
from sheetbend_any._sandbox.native import _load_commands
from sheetbend_any._sandbox.platforms import linux_command, macos_profile, worker_environment
from sheetbend_any.command_plan import discover_commands
from sheetbend_any.errors import AnyError, ContractError, ExecutionError, SandboxError
from sheetbend_any.sandbox import _snapshot_output, _supervise


@pytest.fixture(scope="module")
def shell_sandbox():
    sandbox = Sandbox(limits=Limits(timeout_seconds=5))
    try:
        report = sandbox.doctor()
        assert report["shell_canaries_passed"]
    except (AnyError, OSError) as exc:
        if os.environ.get("ANY_REQUIRE_SANDBOX") == "1":
            pytest.fail(f"Required shell sandbox did not pass: {exc}")
        pytest.skip(f"Actual shell sandbox unavailable: {exc}")
    return sandbox


def test_native_mounts_keep_executables_separate_from_data_grants(tmp_path):
    control, work = tmp_path / "control", tmp_path / "work"
    control.mkdir()
    work.mkdir()
    output = tmp_path / "result"
    output.touch()
    # A synthetic executable outside any normal runtime root catches accidental
    # grants of its parent, user home, or the complete working directory.
    executable = tmp_path / "installed-tools" / "cat"
    executable.parent.mkdir()
    executable.touch()
    command, _, _, _ = linux_command(
        Path("/usr/bin/bwrap"), control=control, work=work, output=output,
        inputs=[], scratch_bytes=4096, shell_executables=(executable,),
    )
    binds = [(command[index + 1], command[index + 2])
             for index, token in enumerate(command) if token == "--ro-bind"]
    assert (str(executable), str(executable)) in binds
    assert not any(source == str(executable.parent) for source, _ in binds)
    assert command.count("--bind") == 1
    assert "--unshare-all" in command and "--die-with-parent" in command


def test_shell_macos_policy_allows_children_but_no_ambient_exec_or_network(tmp_path):
    for name in ("control", "work"):
        (tmp_path / name).mkdir()
    output = tmp_path / "result"
    output.touch()
    bash = tmp_path / "installed" / "bash"
    bash.parent.mkdir()
    bash.touch()
    policy = macos_profile(tmp_path / "control", tmp_path / "work", output, [],
                           shell_executables=(bash,))
    assert "(allow process-fork)" in policy and "(deny process-fork)" not in policy
    assert '(deny network*)' in policy and '(deny system-socket)' in policy
    assert f'(literal "{bash}")' in policy
    assert f'(subpath "{bash.parent}")' not in policy
    assert "(allow process-exec)" not in policy
    assert '(subpath "/")' not in policy


def test_shell_environment_drops_startup_and_loader_overrides(monkeypatch):
    for key in ("BASH_ENV", "ENV", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "sensitive-value")
    environment = worker_environment("/work", shell=True)
    assert not set(environment).intersection({"BASH_ENV", "ENV", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "AWS_SECRET_ACCESS_KEY"})
    assert environment["HOME"] == environment["TMPDIR"] == "/work"
    assert environment["LC_ALL"] == "C"


def test_macho_dependency_reader_handles_spaces_and_rpaths():
    libraries, rpaths = _load_commands("""
Load command 1
          cmd LC_LOAD_DYLIB
      cmdsize 88
         name /opt/runtime with spaces/lib.dylib (offset 24)
Load command 2
          cmd LC_RPATH
      cmdsize 40
         path @loader_path/../lib (offset 12)
Load command 3
          cmd LC_ID_DYLIB
         name /not/a/dependency (offset 24)
""")
    assert libraries == ["/opt/runtime with spaces/lib.dylib"]
    assert rpaths == ["@loader_path/../lib"]


def test_signal_failure_names_the_signal(tmp_path):
    with pytest.raises(ExecutionError, match="SIGTERM"):
        _supervise(
            [sys.executable, "-S", "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
            env=worker_environment(str(tmp_path)), cwd=tmp_path, work=tmp_path,
            limits=Limits(timeout_seconds=2),
        )


def test_frozen_command_identity_change_is_rejected(monkeypatch):
    monkeypatch.setattr("sheetbend_any.sandbox.require_backend", lambda: Path("/synthetic/bwrap"))
    inventory = discover_commands(["bash", "cat"])
    if not all(item["available"] for item in inventory.values()):
        pytest.skip("Bash/cat unavailable")
    for item in inventory.values():
        info = Path(item["path"]).stat()
        item["identity"] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    inventory["cat"]["identity"][2] += 1
    with pytest.raises(ContractError, match="changed identity"):
        with Sandbox().execute_shell([{"argv": ["cat"]}], inputs=[], parameters={}, context={}, inventory=inventory):
            pytest.fail("Changed tools were allowed to execute")


def test_event_verbosity_is_filtered_before_callback():
    events = []
    sandbox = Sandbox(verbose=1, on_event=events.append)
    sandbox._event("command", level=1, script="payload")
    sandbox._event("sandbox.launch", level=2, argv=["private-detail"])
    assert events == [{"event": "command", "level": 1, "script": "payload"}]


def test_snapshot_cannot_be_changed_through_retained_worker_descriptor(tmp_path):
    source, snapshot = tmp_path / "worker-output", tmp_path / "candidate"
    source.write_bytes(b"before")
    with source.open("r+b") as retained:
        _snapshot_output(source, snapshot, max_bytes=64)
        retained.write(b"damage")
        retained.flush()
    assert source.read_bytes() == b"damage"
    assert snapshot.read_bytes() == b"before"
    assert source.stat().st_ino != snapshot.stat().st_ino


def test_snapshot_rejects_symlinks_and_oversized_files(tmp_path):
    source, snapshot = tmp_path / "worker-output", tmp_path / "candidate"
    source.write_bytes(b"too-large")
    with pytest.raises(ExecutionError, match="bounded"):
        _snapshot_output(source, snapshot, max_bytes=2)
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(OSError):
        _snapshot_output(alias, snapshot, max_bytes=64)
    assert not snapshot.exists()


def test_private_workspace_cannot_overlap_a_runtime_read_grant(monkeypatch, tmp_path):
    monkeypatch.setattr("sheetbend_any.sandbox.require_backend", lambda: Path("/synthetic/bwrap"))
    monkeypatch.setattr("sheetbend_any.sandbox.runtime_paths", lambda: ([tmp_path], []))
    with pytest.raises(SandboxError, match="runtime read grant"):
        with Sandbox(temporary_directory=tmp_path).execute("unused", inputs=[], parameters={}, context={}):
            pytest.fail("Private publication snapshot would be exposed through a runtime grant")


def test_supervisor_stops_background_child_before_returning(tmp_path):
    import psutil
    code = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(30)'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
time.sleep(0.15)
"""
    stdout, _, _ = _supervise(
        [sys.executable, "-S", "-c", code], env=worker_environment(str(tmp_path)),
        cwd=tmp_path, work=tmp_path, limits=Limits(timeout_seconds=3),
    )
    try:
        child = psutil.Process(int(stdout))
    except psutil.NoSuchProcess:
        return
    try:
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            child.kill()


def test_real_shell_seccomp_execs_script_and_preserves_denials(tmp_path):
    """Native filter component only: this does not assert filesystem isolation."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux libseccomp component test")
    import ctypes
    try:
        ctypes.CDLL("libseccomp.so.2")
    except OSError:
        pytest.skip("libseccomp not installed")
    worker = Path(__file__).parents[1] / "src/sheetbend_any/_sandbox/worker.py"
    child = tmp_path / "child.py"
    child.write_text("""
import errno, json, os, socket
results = []
for operation in (socket.socket, lambda: os.unshare(os.CLONE_NEWUSER), os.setsid):
    try:
        operation()
    except OSError as exc:
        results.append(exc.errno == errno.EPERM)
    else:
        results.append(False)
print(json.dumps(results))
""")
    code = f"""
import importlib.util, subprocess, sys
spec = importlib.util.spec_from_file_location('component_probe', {str(worker)!r})
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
worker.install_seccomp(shell=True)
result = subprocess.run([sys.executable, '-S', '-s', '-P', {str(child)!r}], check=False)
raise SystemExit(result.returncode)
"""
    result = subprocess.run([sys.executable, "-S", "-s", "-P", "-c", code],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, True, True]


@pytest.mark.sandbox
def test_real_native_pipeline_stages_and_cleans_result(shell_sandbox, tmp_path):
    source = tmp_path / "input with spaces.txt"
    source.write_text("b\na\na\n")
    pipeline = [{"argv": ["cat", {"input": 0}]}, {"argv": ["sort"]}, {"argv": ["uniq"]}]
    with shell_sandbox.execute_shell(pipeline, inputs=[source], parameters={}, context={}) as result:
        staged = result.path
        assert staged.read_bytes() == b"a\nb\n"
        assert result.command and "any_pipeline_status" in result.command
        assert result.tools["cat"]["identity"]
        # Simulate an unseen descendant writing its retained output inode after
        # the context yields: validation/publication must still see frozen bytes.
        worker_inode = staged.with_name("result.bin")
        with worker_inode.open("r+b") as retained:
            retained.write(b"late-worker-mutation")
            retained.flush()
        assert staged.read_bytes() == b"a\nb\n"
    assert not staged.exists()
    assert source.read_text() == "b\na\na\n"


@pytest.mark.sandbox
def test_real_optional_jq_uses_declared_json_input(shell_sandbox, tmp_path):
    if shutil.which("jq") is None:
        pytest.skip("Optional jq is not installed")
    source = tmp_path / "input.json"
    source.write_text('[{"score": 2}, {"score": 7}]')
    pipeline = [{"argv": ["jq", "-c", "map(select(.score > 3))", {"input": 0}]}]
    with shell_sandbox.execute_shell(pipeline, inputs=[source], parameters={}, context={}) as result:
        assert json.loads(result.path.read_bytes()) == [{"score": 7}]


@pytest.mark.sandbox
def test_real_preflight_never_executes_generated_pipeline(shell_sandbox):
    pipeline = [{"argv": ["awk", 'BEGIN { print "TASK_EXECUTED"; exit 42 }']}]
    with shell_sandbox.execute_shell(pipeline, inputs=[], parameters={}, context={}, preflight=True) as result:
        assert result.path.read_bytes() == b""
        assert "TASK_EXECUTED" in result.command


@pytest.mark.sandbox
def test_real_pipeline_upstream_failure_never_yields(shell_sandbox, tmp_path):
    source = tmp_path / "input"
    source.write_text("example\n")
    pipeline = [{"argv": ["grep", "[", {"input": 0}]}, {"argv": ["wc", "-l"]}]
    with pytest.raises(ExecutionError, match="stage 1"):
        with shell_sandbox.execute_shell(pipeline, inputs=[source], parameters={}, context={}):
            pytest.fail("Upstream command failure exposed a result")


@pytest.mark.sandbox
def test_real_no_match_is_valid_empty_output(shell_sandbox, tmp_path):
    source = tmp_path / "input"
    source.write_text("example\n")
    with shell_sandbox.execute_shell([{"argv": ["grep", "absent", {"input": 0}]}],
                                    inputs=[source], parameters={}, context={}) as result:
        assert result.path.read_bytes() == b""


@pytest.mark.sandbox
def test_real_native_command_cannot_write_input(shell_sandbox, tmp_path):
    source = tmp_path / "input"
    source.write_text("original\n")
    pipeline = [{"argv": ["awk", 'BEGIN { print "damage" > ARGV[1] }', {"input": 0}]}]
    with pytest.raises(ExecutionError):
        with shell_sandbox.execute_shell(pipeline, inputs=[source], parameters={}, context={}):
            pytest.fail("Native command wrote a read-only input")
    assert source.read_text() == "original\n"


@pytest.mark.sandbox
def test_real_native_command_cannot_read_undeclared_file(shell_sandbox, tmp_path):
    hidden = tmp_path / "private"
    hidden.write_text("private-canary\n")
    with pytest.raises(ExecutionError):
        with shell_sandbox.execute_shell([{"argv": ["cat", str(hidden)]}],
                                        inputs=[], parameters={}, context={}):
            pytest.fail("Native command read an undeclared file")


@pytest.mark.sandbox
def test_real_native_pipeline_receives_no_ambient_secrets(shell_sandbox, monkeypatch):
    monkeypatch.setenv("ANY_TEST_TOKEN", "private-canary")
    monkeypatch.setenv("BASH_ENV", "/unused-startup")
    pipeline = [{"argv": ["awk", 'BEGIN { print ENVIRON["ANY_TEST_TOKEN"]; print ENVIRON["BASH_ENV"] }']}]
    with shell_sandbox.execute_shell(pipeline, inputs=[], parameters={}, context={}) as result:
        assert result.path.read_bytes() == b"\n\n"


@pytest.mark.sandbox
def test_real_native_pipeline_timeout_never_yields(shell_sandbox):
    sandbox = Sandbox(limits=Limits(timeout_seconds=0.5))
    pipeline = [{"argv": ["awk", "BEGIN { while (1) {} }"]}]
    with pytest.raises(ExecutionError, match="time limit|status"):
        with sandbox.execute_shell(pipeline, inputs=[], parameters={}, context={}):
            pytest.fail("Unfinished pipeline yielded a staged result")
