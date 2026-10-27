"""Authored Bash composition tests, not operating-system confinement tests."""

import json
import os
from pathlib import Path
import shutil
import subprocess

from jsonschema import Draft202012Validator
import pytest

from sheetbend_any.command_plan import (
    COMMANDS, discover_commands, render_pipeline, resolve_pipeline, validate_pipeline,
)
from sheetbend_any.contracts import load_schema
from sheetbend_any.errors import ContractError


def _run(pipeline, inputs=(), parameters=None, context=None):
    """Run only test-authored native commands with no shell startup environment."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Native Bash not available for renderer tests.")
    inventory = discover_commands([stage["argv"][0] for stage in pipeline])
    if any(not entry["available"] for entry in inventory.values()):
        pytest.skip("An authored renderer-test command is unavailable.")
    argv = resolve_pipeline(pipeline, inputs, parameters or {}, context or {}, inventory)
    script = render_pipeline(argv)
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-c", script],
        capture_output=True, timeout=10, env={"PATH": os.defpath, "LC_ALL": "C"},
    )


def test_discovery_is_bounded_metadata_and_does_not_execute(tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    executable = tmp_path / "jq"
    executable.write_text(f"#!/bin/sh\ntouch {marker}\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    inventory = discover_commands(["jq", "not-a-supported-command", "jq"])
    assert list(inventory) == ["jq", "not-a-supported-command"]
    assert inventory["jq"]["path"] == str(executable)
    assert inventory["jq"]["available"] is True
    assert inventory["jq"]["features_verified"] is False
    assert inventory["not-a-supported-command"]["supported"] is False
    assert not marker.exists()
    with pytest.raises(ContractError, match="32"):
        discover_commands(["cat"] * 33)
    with pytest.raises(ContractError):
        discover_commands("cat")


def test_discovery_ignores_relative_path_entries(tmp_path, monkeypatch):
    executable = tmp_path / "cat"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ":.:relative")
    assert discover_commands(["cat"])["cat"]["available"] is False


def test_runtime_bash_is_not_a_pipeline_command():
    assert discover_commands(["bash"])["bash"]["pipeline_allowed"] is False
    with pytest.raises(ContractError, match="command name"):
        validate_pipeline([{"argv": ["bash", "-c", "echo unsafe"]}], 0, {})


@pytest.mark.parametrize("pipeline", [
    [], [{"argv": ["/bin/cat"]}], [{"argv": ["cat"], "shell": "extra"}],
    [{"argv": ["cat", {"input": 1}]}], [{"argv": ["cat", {"input": True}]}],
    [{"argv": ["cat", {"param": "unknown"}]}], [{"argv": ["cat", {"context": "cwd"}]}],
    [{"argv": ["cat", {"input": 0, "param": "x"}]}], [{"argv": ["cat", 3]}],
    [{"argv": ["cat", "bad\x00value"]}], [{"argv": ["cat", "x" * 8193]}],
])
def test_invalid_compositions_are_rejected(pipeline):
    with pytest.raises(ContractError):
        validate_pipeline(pipeline, 1, {})


@pytest.mark.parametrize("argv", [
    ["find", {"input": 0}, "-delete"], ["find", {"input": 0}, "-exec", "cat", "{}", ";"],
    ["sed", "-i", "s/a/b/", {"input": 0}], ["sort", "--output=elsewhere"],
    ["sort", "--compress-program=program"], ["tail", "-f", {"input": 0}],
    ["awk", "--load", "library", "{print}"], ["mlr", "-I", "cat"],
    ["sort", "-roelsewhere"], ["sort", "-sTelsewhere"],
    ["find", "--", {"input": 0}, "-delete"],
])
def test_known_side_effect_options_are_rejected(argv):
    with pytest.raises(ContractError, match="outside"):
        validate_pipeline([{"argv": argv}], 1, {})


def test_parameter_options_are_checked_after_binding(tmp_path):
    inventory = {"find": {"available": True, "path": "/usr/bin/find"}}
    with pytest.raises(ContractError, match="outside"):
        resolve_pipeline(
            [{"argv": ["find", {"input": 0}, {"param": "operation"}]}],
            [tmp_path], {"operation": "-delete"}, {}, inventory,
        )


def test_resolution_uses_literal_references_and_frozen_inventory(tmp_path, monkeypatch):
    pipeline = [{"argv": ["printf", "%s|%s|%s", {"input": 0}, {"param": "text"}, {"context": "now"}]}]
    filename = tmp_path / "a file;$(touch nope)"
    inventory = {"printf": {"available": True, "path": "/usr/bin/printf"}}
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: pytest.fail("Must use frozen inventory"))
    result = resolve_pipeline(pipeline, [filename], {"text": "a'quoted\nvalue"}, {"now": "fixed"}, inventory)
    assert result == [["/usr/bin/printf", "%s|%s|%s", str(filename), "a'quoted\nvalue", "fixed"]]


def test_missing_command_fails_before_execution():
    with pytest.raises(ContractError, match="unavailable"):
        resolve_pipeline([{"argv": ["cat"]}], [], {}, {}, {"cat": {"available": False, "path": None}})


def test_literal_shell_metacharacters_are_never_executed(tmp_path):
    marker = tmp_path / "must-not-exist"
    value = f"$(touch {marker}); 'quoted' `echo wrong`\n* $HOME"
    result = _run([{"argv": ["printf", "%s", {"param": "value"}]}], parameters={"value": value})
    assert result.returncode == 0, result.stderr
    assert result.stdout == value.encode()
    assert not marker.exists()


def test_pipeline_works_with_spaced_input_names(tmp_path):
    source = tmp_path / "name with spaces;quotes'.txt"
    source.write_text("b\na\nb\n")
    result = _run([{"argv": ["sort", {"input": 0}]}, {"argv": ["uniq", "-c"]}], [source])
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [b"1", b"a", b"2", b"b"]


def test_grep_empty_match_is_successful_empty_output():
    result = _run([{"argv": ["printf", "alpha\n"]}, {"argv": ["grep", "-e", "missing"]}])
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


def test_invalid_grep_pattern_is_a_failure():
    result = _run([{"argv": ["printf", "alpha\n"]}, {"argv": ["grep", "-e", "["]}])
    assert result.returncode == 1
    assert b"stage 2 (grep) exited 2" in result.stderr


def test_upstream_error_is_not_hidden_by_successful_last_stage(tmp_path):
    result = _run([{"argv": ["cat", {"input": 0}]}, {"argv": ["wc", "-l"]}], [tmp_path / "missing"])
    assert result.returncode == 1
    assert b"stage 1 (cat) exited 1" in result.stderr


@pytest.mark.parametrize("consumer", [
    ["head", "-n", "1"], ["grep", "-q", "needle"], ["grep", "-m1", "needle"],
    ["grep", "-m0", "needle"], ["grep", "--max-count", "0", "needle"],
])
def test_supported_early_consumer_accepts_expected_upstream_sigpipe(tmp_path, consumer):
    source = tmp_path / "large.txt"
    source.write_bytes(b"needle\n" * 300000)
    result = _run([{"argv": ["cat", {"input": 0}]}, {"argv": consumer}], [source])
    assert result.returncode == 0, result.stderr


def test_short_consumer_does_not_hide_real_upstream_failure(tmp_path):
    result = _run([{"argv": ["cat", {"input": 0}]}, {"argv": ["head", "-n", "0"]}], [tmp_path / "missing"])
    assert result.returncode == 1
    assert b"stage 1 (cat) exited 1" in result.stderr


def test_unsupported_sigpipe_does_not_pass():
    result = _run([{"argv": ["awk", "BEGIN { exit 141 }"]}, {"argv": ["wc", "-c"]}])
    assert result.returncode == 1
    assert b"stage 1 (awk) exited 141" in result.stderr


@pytest.mark.parametrize("pattern_args", [["-e", "-q"], ["-e-m"], ["--regexp", "--quiet"]])
def test_grep_pattern_is_not_mistaken_for_short_consumer(pattern_args):
    result = _run([{"argv": ["awk", "BEGIN { exit 141 }"]}, {"argv": ["grep", *pattern_args]}])
    assert result.returncode == 1
    assert b"stage 1 (awk) exited 141" in result.stderr


def test_later_grep_maximum_overrides_zero_for_sigpipe_handling():
    result = _run([
        {"argv": ["awk", "BEGIN { exit 141 }"]},
        {"argv": ["grep", "-m0", "-m1", "missing"]},
    ])
    assert result.returncode == 1
    assert b"stage 1 (awk) exited 141" in result.stderr


def _shell_document():
    return {
        "schema_version": 2, "kind": "shell", "description": "Count input bytes.",
        "code": "", "pipeline": [{"argv": ["wc", "-c", {"input": 0}]}], "fallback_reason": "",
        "inputs": [{"kind": "file", "format": "bytes", "required_columns": []}],
        "output": {"format": "text", "required_columns": []}, "parameters": {}, "tests": [],
    }


def test_schema_accepts_legacy_recipe_and_new_shell_document(recipe):
    schema = load_schema("recipe")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(recipe.to_dict())
    validator.validate(_shell_document())
    assert schema["$defs"]["v2"]["properties"]["pipeline"]["items"]["properties"]["argv"]["prefixItems"][0]["enum"] == list(COMMANDS)


def test_schema_prevents_shell_code_and_unexplained_python_fallback(recipe):
    validator = Draft202012Validator(load_schema("recipe"))
    shell = _shell_document()
    shell["code"] = "print('hidden Python')"
    assert not validator.is_valid(shell)
    fallback = recipe.to_dict()
    fallback.pop("runtime")
    fallback.update(schema_version=2, kind="python", pipeline=[], fallback_reason="")
    assert not validator.is_valid(fallback)
    fallback["fallback_reason"] = "CSV-aware command unavailable; Python csv preserves quoted fields."
    validator.validate(fallback)


def test_planner_schema_requires_complete_action_shapes():
    path = Path(__file__).parents[1] / "src/sheetbend_any/schemas/planner.schema.json"
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate({"action": "tools", "commands": ["jq"], "recipe": None})
    validator.validate({"action": "final", "commands": [], "recipe": _shell_document()})
    validator.validate({"action": "final", "commands": [], "recipe": {
        "kind": "shell", "pipeline": [{"argv": ["sort", {"input": 0}]}],
    }})
    assert not validator.is_valid({"action": "final", "commands": [], "recipe": {
        "kind": "shell", "pipeline": [{"argv": ["sort", {"input": 0}]}],
        "permissions": {"write_home": True},
    }})
    assert not validator.is_valid({"action": "tools", "commands": [], "recipe": None})
    assert not validator.is_valid({"action": "final", "commands": [], "recipe": None})
