"""Shell CLI orchestration only; the authored double never evaluates shell code."""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from sheetbend_any import Recipe, Request
from sheetbend_any import cli
from sheetbend_any.command_plan import render_pipeline, resolve_pipeline
from sheetbend_any.compiler import GeneratedRecipe
from sheetbend_any.errors import ExecutionError, SandboxError
from sheetbend_any.sandbox import StagedResult


@pytest.fixture
def shell_case(tmp_path):
    source = tmp_path / "lines.txt"
    source.write_text("beta\nalpha\n")
    document = {
        "schema_version": 2, "kind": "shell", "description": "Sort the declared lines.",
        "code": "", "pipeline": [{"argv": ["sort", {"input": 0}]}],
        "fallback_reason": "",
        "inputs": [{"kind": "file", "format": "text", "required_columns": []}],
        "output": {"format": "text", "required_columns": []}, "parameters": {}, "tests": [],
    }
    recipe_file = tmp_path / "sort.recipe.json"
    recipe_file.write_text(json.dumps(document))
    return SimpleNamespace(
        source=source, document=document, recipe_file=recipe_file,
        options=["--recipe-file", str(recipe_file), "-i", str(source)],
    )


@pytest.fixture
def shell_boundary_double(monkeypatch):
    """Observe CLI calls and stage authored bytes; never run arbitrary recipe code."""
    state = {
        "instances": [], "available_checks": 0, "preflights": [], "executions": [],
        "unavailable": False, "preflight_failure": False, "execution_failure": False,
        "output": b"alpha\nbeta\n",
    }

    class ShellBoundaryDouble:
        def __init__(self, *, limits, temporary_directory, on_event=None, verbose=0):
            self.limits = limits
            self.temporary_directory = temporary_directory
            self.on_event = on_event
            self.verbose = verbose
            state["instances"].append(self)

        def require_available(self):
            state["available_checks"] += 1
            if state["unavailable"]:
                raise SandboxError("Synthetic unavailable execution boundary.")

        def fingerprint(self):
            return "CLI-ORCHESTRATION-DOUBLE-NOT-A-SANDBOX"

        def _emit(self, detail):
            if self.on_event is not None:
                self.on_event(detail)

        def _command(self, pipeline, parameters, context, inventory, *, preflight=False):
            # These paths intentionally differ from the host-bound preview.
            visible_inputs = ["/synthetic-inputs/0/lines.txt"]
            script = render_pipeline(resolve_pipeline(
                pipeline, visible_inputs, parameters, context, inventory=inventory,
            ))
            if context.get("any_phase") == "fixture":
                script = "# SYNTHETIC FIXTURE PAYLOAD\n" + script
            self._emit({
                "event": "command", "level": 1, "script": script,
                "inputs": visible_inputs, "preflight": preflight,
                "phase": context.get("any_phase"),
            })
            return script, visible_inputs

        def preflight_shell(self, pipeline, *, inputs, parameters, context, inventory):
            state["preflights"].append({"pipeline": pipeline, "inputs": inputs})
            self._emit({"event": "boundary", "level": 2, "backend": "synthetic-double"})
            self._command(pipeline, parameters, context, inventory, preflight=True)
            if state["preflight_failure"]:
                raise SandboxError("Synthetic boundary probe failed.")
            return {"backend": "synthetic-double", "canaries_passed": True, "task_executed": False}

        @contextmanager
        def execute_shell(self, pipeline, *, inputs, parameters, context, output_suffix, inventory):
            script, visible_inputs = self._command(pipeline, parameters, context, inventory)
            state["executions"].append({
                "pipeline": pipeline, "script": script, "inputs": visible_inputs,
                "phase": context.get("any_phase"),
            })
            with tempfile.TemporaryDirectory(prefix="any-cli-authored-fixture-") as temporary:
                output = Path(temporary) / ("result" + output_suffix)
                output.write_bytes(state["output"])
                if state["execution_failure"]:
                    raise ExecutionError("Synthetic pipeline failed after staging bytes.", returncode=2)
                yield StagedResult(
                    output, b"", b"", 0.001, "CLI-ORCHESTRATION-DOUBLE-NOT-A-SANDBOX",
                    command=script, tools=inventory,
                )

        def execute(self, *args, **kwargs):
            pytest.fail("Shell CLI must not enter Python execution.")

    monkeypatch.setattr(cli, "Sandbox", ShellBoundaryDouble)
    return state


def test_default_shell_stdout_contains_only_result(shell_case, shell_boundary_double, cli_runner):
    result = cli_runner.invoke(cli.main, [*shell_case.options, "-o", "-", "--no-cache"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "alpha\nbeta\n" and result.stderr == ""
    assert len(shell_boundary_double["preflights"]) == 1
    assert len(shell_boundary_double["executions"]) == 1


def test_v_prints_exact_runtime_payload_not_host_preview(shell_case, shell_boundary_double, cli_runner):
    result = cli_runner.invoke(cli.main, [*shell_case.options, "-o", "-", "--no-cache", "-v"])
    assert result.exit_code == 0, result.output
    run = shell_boundary_double["executions"][0]
    expected = (
        "# Bash payload; stdout is staged for authorized publication.\n"
        "# input 0: /synthetic-inputs/0/lines.txt\n"
        + run["script"]
    )
    assert result.stdout == "alpha\nbeta\n"
    assert result.stderr == expected
    assert str(shell_case.source) not in result.stderr
    assert "Preflight candidate" not in result.stderr


def test_v_hides_fixture_commands(shell_case, shell_boundary_double, cli_runner):
    shell_case.document["tests"] = [{
        "name": "sort", "files": [{"input_index": 0, "path": "", "content": "beta\nalpha\n"}],
        "parameters": {}, "expected_output": "alpha\nbeta\n",
    }]
    shell_case.recipe_file.write_text(json.dumps(shell_case.document))
    result = cli_runner.invoke(cli.main, [*shell_case.options, "-o", "-", "--no-cache", "-v"])
    assert result.exit_code == 0, result.output
    assert [row["phase"] for row in shell_boundary_double["executions"]] == ["fixture", None]
    assert "SYNTHETIC FIXTURE PAYLOAD" not in result.stderr
    assert result.stdout == "alpha\nbeta\n"


def test_vv_adds_workflow_diagnostics(shell_case, shell_boundary_double, cli_runner):
    result = cli_runner.invoke(cli.main, [*shell_case.options, "-o", "-", "--no-cache", "-vv"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "alpha\nbeta\n"
    assert "Checking 0 synthetic fixture(s)." in result.stderr
    assert "Validating the staged result" in result.stderr and "Published 11 bytes." in result.stderr
    assert '"backend": "synthetic-double"' in result.stderr
    assert shell_boundary_double["executions"][0]["script"] in result.stderr


@pytest.mark.parametrize("flag", ["--dry-run", "--dryrun"])
def test_dryrun_does_io_and_boundary_preflight_without_candidate(
    flag, shell_case, shell_boundary_double, tmp_path, monkeypatch, cli_runner,
):
    observed = []
    original = Request.preflight

    def observe(request, *, probe_output=True):
        report = original(request, probe_output=probe_output)
        observed.append((probe_output, report))
        return report

    monkeypatch.setattr(Request, "preflight", observe)
    destination = tmp_path / "sorted.txt"
    result = cli_runner.invoke(cli.main, [
        *shell_case.options, "-o", str(destination), "--no-cache", flag,
    ])
    assert result.exit_code == 0, result.output
    assert result.stdout == "" and "Preflight passed" in result.stderr
    assert observed and all(report["inputs"][0]["readable"] for _, report in observed)
    assert any(report["output_parent_probe"] for _, report in observed)
    assert shell_boundary_double["available_checks"] > 0
    assert len(shell_boundary_double["preflights"]) == 1
    assert shell_boundary_double["executions"] == []
    assert not destination.exists() and not list(tmp_path.glob(".any-preflight-*"))


def test_plan_only_compiles_with_unavailable_sandbox_without_write_probe(
    shell_case, shell_boundary_double, tmp_path, monkeypatch, cli_runner,
):
    shell_boundary_double["unavailable"] = True
    observed = []
    generated = []
    constructor_options = []
    original = Request.preflight

    def observe(request, *, probe_output=True):
        observed.append(probe_output)
        return original(request, probe_output=probe_output)

    class AuthoredCompilerDouble:
        def __init__(self, **options):
            constructor_options.append(options)

        def cache_identity(self):
            return {"double": "authored-CLI-plan"}

        def generate(self, request, inspected):
            generated.append((request.task, inspected))
            return GeneratedRecipe(Recipe(shell_case.document), {"test_double": True})

    monkeypatch.setattr(Request, "preflight", observe)
    monkeypatch.setattr(cli, "SheetbendCompiler", AuthoredCompilerDouble)
    destination = tmp_path / "sorted.txt"
    result = cli_runner.invoke(cli.main, [
        "sort the lines", "-i", str(shell_case.source), "-o", str(destination),
        "--plan-only", "--planning-attempts", "3", "--no-cache",
    ])
    assert result.exit_code == 0, result.output
    assert len(generated) == 1 and constructor_options[0]["max_attempts"] == 3
    assert observed and not any(observed)
    assert shell_boundary_double["available_checks"] == 0
    assert shell_boundary_double["preflights"] == shell_boundary_double["executions"] == []
    assert result.stdout == "" and "Plan only" in result.stderr and not destination.exists()


def test_list_tools_is_offline_and_does_not_probe_executables(tmp_path, monkeypatch, cli_runner):
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata inventory must not construct a compiler/sandbox or spawn a process.")

    monkeypatch.setattr(cli, "Sandbox", forbidden)
    monkeypatch.setattr(cli, "SheetbendCompiler", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("SHEETBEND_CONFIG", str(tmp_path / "missing-config.toml"))
    result = cli_runner.invoke(cli.main, ["--list-tools"])
    assert result.exit_code == 0, result.output
    inventory = json.loads(result.stdout)
    assert "sort" in inventory and inventory["sort"]["features_verified"] is False
    assert result.stderr == "" and not (tmp_path / "cache-base").exists()


def test_history_is_offline_and_contains_the_executed_payload(
    shell_case, shell_boundary_double, monkeypatch, cli_runner,
):
    executed = cli_runner.invoke(cli.main, [*shell_case.options, "-o", "-"])
    assert executed.exit_code == 0, executed.output

    def forbidden(*args, **kwargs):
        pytest.fail("History must not construct a compiler or sandbox.")

    monkeypatch.setattr(cli, "Sandbox", forbidden)
    monkeypatch.setattr(cli, "SheetbendCompiler", forbidden)
    result = cli_runner.invoke(cli.main, ["--history"])
    assert result.exit_code == 0, result.output
    records = json.loads(result.stdout)
    assert len(records) == 1 and records[0]["published"] is True
    assert records[0]["execution_kind"] == "shell"
    assert records[0]["command"] == shell_boundary_double["executions"][0]["script"]
    assert result.stderr == ""


@pytest.mark.parametrize("failure", ["preflight", "execution", "validation"])
def test_failure_never_publishes_stdout_or_final_file(
    failure, shell_case, shell_boundary_double, tmp_path, cli_runner,
):
    if failure == "preflight":
        shell_boundary_double["preflight_failure"] = True
    elif failure == "execution":
        shell_boundary_double["execution_failure"] = True
    else:
        shell_boundary_double["output"] = b"\xffinvalid UTF-8"
    for destination in ("-", str(tmp_path / "result.txt")):
        options = [*shell_case.options, "-o", destination, "--no-cache"]
        if failure == "preflight":
            options.append("--dryrun")
        result = cli_runner.invoke(cli.main, options)
        assert result.exit_code == 1, result.output
        assert result.stdout == "" and "Error:" in result.stderr
        assert not (tmp_path / "result.txt").exists()
    if failure == "preflight":
        assert shell_boundary_double["executions"] == []
