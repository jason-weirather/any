"""Actual OS tests are clearly marked. Unavailable backends are skips, never passes."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sheetbend_any import Limits, Sandbox
from sheetbend_any._sandbox.platforms import linux_command, macos_profile
from sheetbend_any.errors import AnyError, ExecutionError, SandboxError
from sheetbend_any.testing import check_recipe


@pytest.fixture
def actual_sandbox():
    sandbox = Sandbox(limits=Limits(timeout_seconds=5))
    try:
        sandbox.doctor()
    except (AnyError, OSError) as exc:
        if os.environ.get("ANY_REQUIRE_SANDBOX") == "1":
            pytest.fail(f"Required OS sandbox did not pass: {exc}")
        pytest.skip(f"Actual OS sandbox unavailable: {exc}")
    return sandbox


def test_linux_command_mounts_only_one_writable_host_file(tmp_path):
    control, work = tmp_path / "control", tmp_path / "work"
    control.mkdir()
    work.mkdir()
    out, source = tmp_path / "out", tmp_path / "in"
    out.touch()
    source.touch()
    command, inputs, output, scratch = linux_command(
        Path("/usr/bin/bwrap"), control=control, work=work, output=out,
        inputs=[source], scratch_bytes=1000,
    )
    assert "--unshare-all" in command and "--clearenv" in command
    assert command.count("--bind") == 1
    assert command[command.index("--bind") + 1] == str(out)
    assert "--size" in command and "--remount-ro" in command
    assert "--ro-bind" in command and str(source) in command
    assert scratch == "/work" and inputs == ["/inputs/0/in"]


def test_macos_profile_escapes_paths_and_denies_broad_effects(tmp_path):
    control, work = tmp_path / 'control"quoted', tmp_path / "work"
    control.mkdir()
    work.mkdir()
    out = tmp_path / "out"
    out.touch()
    profile = macos_profile(control, work, out, [])
    assert "(deny default)" in profile
    assert '(deny network*)' in profile and '(deny process-fork)' in profile
    assert '(deny system-socket)' in profile
    assert '\\"quoted' in profile
    assert "(allow default)" not in profile
    assert "(allow process-fork)" not in profile


def test_missing_backend_fails_closed(monkeypatch):
    import sheetbend_any._sandbox.platforms as platforms
    monkeypatch.setattr(platforms.sys, "platform", "linux")
    monkeypatch.setattr(platforms.shutil, "which", lambda name: None)
    with pytest.raises(SandboxError, match="No unsandboxed fallback"):
        Sandbox().require_available()


def test_real_linux_seccomp_denies_network_process_creation_and_metadata_mutation(tmp_path):
    """Native syscall-filter component test, not a full filesystem isolation test."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux libseccomp component test")
    import ctypes
    try:
        ctypes.CDLL("libseccomp.so.2")
    except OSError:
        pytest.skip("libseccomp not installed")
    worker = Path(__file__).parents[1] / "src/sheetbend_any/_sandbox/worker.py"
    target = tmp_path / "target"
    target.write_text("unchanged")
    original_mode = target.stat().st_mode
    code = f'''
import importlib.util, json, os, socket
spec = importlib.util.spec_from_file_location("component_probe", {str(worker)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.install_seccomp()
results = []
for fn in (lambda: socket.socket(), lambda: os.fork(), lambda: os.chmod({str(target)!r}, 0o777)):
    try:
        fn()
    except PermissionError:
        results.append(True)
    else:
        results.append(False)
print(json.dumps(results))
'''
    result = subprocess.run([sys.executable, "-S", "-s", "-P", "-c", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, True, True]
    assert target.read_text() == "unchanged" and target.stat().st_mode == original_mode


@pytest.mark.sandbox
def test_real_fixture_program_and_cleanup(actual_sandbox, recipe):
    assert check_recipe(recipe, actual_sandbox).passed == 2


@pytest.mark.sandbox
@pytest.mark.parametrize("attempt", [
    "inputs[0].write_text('damage')",
    "__import__('os').chmod(inputs[0], 0o777)",
    "__import__('socket').socket().connect(('127.0.0.1', 9))",
    "__import__('os').fork()",
    "__import__('subprocess').run(['/bin/echo', 'bad'])",
])
def test_real_worker_cannot_mutate_inputs_or_escape(actual_sandbox, tmp_path, attempt):
    source = tmp_path / "input"
    source.write_text("original")
    mode = source.stat().st_mode
    with pytest.raises(ExecutionError):
        with actual_sandbox.execute(
            f"def run(inputs, output, params, context):\n    {attempt}\n",
            inputs=[source], parameters={}, context={},
        ):
            pytest.fail("Forbidden operation unexpectedly succeeded")
    assert source.read_text() == "original" and source.stat().st_mode == mode


@pytest.mark.sandbox
def test_real_timeout_and_output_buffering(actual_sandbox):
    sandbox = Sandbox(limits=replace(actual_sandbox.limits, timeout_seconds=1))
    with pytest.raises(ExecutionError):
        with sandbox.execute(
            "def run(inputs, output, params, context):\n    output.write_text('partial')\n    while True: pass\n",
            inputs=[], parameters={}, context={},
        ):
            pytest.fail("An unfinished result was exposed")
