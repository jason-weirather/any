"""Planner orchestration doubles and real Sheetbend selection/lifetime checks."""

from contextlib import contextmanager
from copy import deepcopy
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from sheetbend_any import Recipe, Request, SheetbendCompiler
from sheetbend_any.errors import ContractError, GenerationError
from sheetbend_any.diagnostics import bounded_visible, safe_exception_detail


def config_file(tmp_path, *, local=True):
    path = tmp_path / "sheetbend.toml"
    text = '''schema_version = 2
default_source = "institution"
[sources.institution]
protocol = "openai-compatible"
base_url = "https://institution.example.invalid/v1"
default_model = "test"
scope = "institutional"
organization = "test"
auth = {type = "none"}
[sources.institution.models.test.capabilities]
json_schema = true
'''
    if local:
        text += '''
[sources.laptop]
protocol = "openai-compatible"
base_url = "http://127.0.0.1:12345/v1"
default_model = "test"
scope = "local"
auth = {type = "none"}
[sources.laptop.models.test.capabilities]
json_schema = true
[sources.laptop.models.test.reasoning]
control = "fixed-off"
'''
    path.write_text(text)
    return path


def python_document(recipe):
    document = recipe.to_dict()
    document.pop("runtime")
    document.update(
        schema_version=2, kind="python", pipeline=[],
        fallback_reason="The CSV operation requires quoted-field and numeric validation.",
    )
    return document


def shell_document():
    return {
        "schema_version": 2, "kind": "shell", "description": "Sort the input lines.",
        "code": "", "pipeline": [{"argv": ["sort", {"input": 0}]}], "fallback_reason": "",
        "inputs": [{"kind": "file", "format": "text", "required_columns": []}],
        "output": {"format": "text", "required_columns": []}, "parameters": {}, "tests": [],
    }


def final(document):
    return {"action": "final", "commands": [], "recipe": document}


def scripted_inference(monkeypatch, responses):
    """A labeled SDK-free orchestration double; no source/network correctness claim."""
    answers = iter(responses)
    state = {"open": False, "connections": [], "prompts": [], "selections": []}

    class SheetbendError(Exception):
        pass

    class SourceDouble:
        name = "laptop"
        default_model = "test"
        scope = "local"

        def resolve_reasoning(self, reasoning, *, model):
            return SimpleNamespace(to_dict=lambda: {"selected": reasoning or "provider"})

        @contextmanager
        def connect(self, **options):
            state["connections"].append(options)
            state["open"] = True

            def prompt(text, **kwargs):
                assert state["open"]
                state["prompts"].append((text, kwargs))

                def consume():
                    assert state["open"]
                    answer = next(answers)
                    if callable(answer):
                        answer = answer()
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer if isinstance(answer, str) else json.dumps(answer)

                return SimpleNamespace(text=consume)

            try:
                yield SimpleNamespace(prompt=prompt)
            finally:
                state["open"] = False

    class RegistryDouble:
        @classmethod
        def from_file(cls, path):
            return cls()

        def source(self, name, *, allowed_scopes):
            state["selections"].append((name, allowed_scopes))
            return SourceDouble()

    module = ModuleType("sheetbend")
    module.Registry = RegistryDouble
    module.__version__ = "test-double"
    errors = ModuleType("sheetbend.errors")
    errors.SheetbendError = SheetbendError
    monkeypatch.setitem(sys.modules, "sheetbend", module)
    monkeypatch.setitem(sys.modules, "sheetbend.errors", errors)
    return state


def test_tools_then_repaired_candidate_stays_in_one_connection(table, monkeypatch):
    bad = shell_document()
    bad["pipeline"][0]["argv"][1] = {"input": 9}
    state = scripted_inference(monkeypatch, [
        {"action": "tools", "commands": ["jq"], "recipe": None},
        final(bad), final(shell_document()),
    ])
    result = SheetbendCompiler(max_attempts=3).generate(
        Request.bind("sort lines", input_paths=[table], output_path="-"), [],
    )
    assert result.recipe.is_shell and result.provenance["planning_attempts"] == 3
    assert len(state["connections"]) == 1
    assert state["connections"][0]["requires"] == {"json_schema"}
    assert state["selections"] == [(None, ("local",))]
    second = json.loads(state["prompts"][1][0].split("REQUEST DATA:\n", 1)[1])
    third = json.loads(state["prompts"][2][0].split("REQUEST DATA:\n", 1)[1])
    assert "jq" in second["feedback"]["tool_results"]
    assert second["feedback"]["tool_results"]["jq"]["features_verified"] is False
    assert "nonexistent input" in third["feedback"]["validation_error"]
    assert third["task"] == "sort lines" and third["attempts_remaining_after_this"] == 0
    assert not state["open"]


def test_invalid_json_repaired_without_default_response_disclosure(table, monkeypatch):
    state = scripted_inference(monkeypatch, ["PRIVATE_REJECTED_RESPONSE", final(shell_document())])
    events = []
    result = SheetbendCompiler(debug_level=2, on_event=events.append).generate(
        Request.bind("sort", input_paths=[table], output_path="-"), [],
    )
    assert result.recipe.is_shell and len(state["prompts"]) == 2
    log = "\n".join(events)
    assert "candidate rejected" in log and "response=" in log and "llm=" in log
    assert "PRIVATE_REJECTED_RESPONSE" not in log


def test_high_debug_response_is_bounded_and_marked_as_data(table, monkeypatch):
    scripted_inference(monkeypatch, ["PRIVATE_REJECTED_RESPONSE " + "x" * 20000])
    events = []
    with pytest.raises(GenerationError, match="exhausted 1"):
        SheetbendCompiler(max_attempts=1, debug_level=3, on_event=events.append).generate(
            Request.bind("sort", input_paths=[table], output_path="-"), [],
        )
    log = "\n".join(events)
    assert "PRIVATE_REJECTED_RESPONSE" in log
    assert "may include input data" in log and "[diagnostic truncated]" in log
    assert len(log) < 9000


def test_missing_command_is_candidate_feedback_not_unconfined_probe(table, monkeypatch):
    from sheetbend_any import compiler as compiler_module

    seen = []

    def discover(names=None):
        seen.append(names)
        return {
            "jq": {"available": False, "supported": True, "path": None},
            "sort": {"available": True, "supported": True, "path": "/usr/bin/sort"},
        }

    monkeypatch.setattr(compiler_module, "discover_commands", discover)
    missing = shell_document()
    missing["pipeline"] = [{"argv": ["jq", ".", {"input": 0}]}]
    state = scripted_inference(monkeypatch, [final(missing), final(shell_document())])
    result = SheetbendCompiler().generate(
        Request.bind("sort", input_paths=[table], output_path="-"), [],
    )
    assert result.recipe.is_shell and seen == [None]
    second = json.loads(state["prompts"][1][0].split("REQUEST DATA:\n", 1)[1])
    assert "unavailable" in second["feedback"]["validation_error"]


def test_actual_column_contract_feedback_uses_input_index(table, monkeypatch):
    invalid = shell_document()
    invalid["inputs"][0].update(format="csv", required_columns=["missing_column"])
    state = scripted_inference(monkeypatch, [final(invalid), final(shell_document())])
    SheetbendCompiler().generate(Request.bind("sort", input_paths=[table], output_path="-"), [])
    second = json.loads(state["prompts"][1][0].split("REQUEST DATA:\n", 1)[1])
    detail = second["feedback"]["validation_error"]
    assert "missing_column" in detail and "input[0]" in detail
    assert str(table) not in detail


def test_invalid_final_field_feedback_is_specific_without_echoing_value(table, monkeypatch):
    invalid = shell_document()
    invalid["output"]["format"] = "PRIVATE_REJECTED_FORMAT_VALUE"
    state = scripted_inference(monkeypatch, [final(invalid), final(shell_document())])
    events = []
    result = SheetbendCompiler(max_attempts=2, debug_level=2, on_event=events.append).generate(
        Request.bind("sort", input_paths=[table], output_path="-"), [],
    )
    assert result.recipe.is_shell and len(state["prompts"]) == 2
    second = json.loads(state["prompts"][1][0].split("REQUEST DATA:\n", 1)[1])
    detail = second["feedback"]["validation_error"]
    assert "output.format" in detail and "enum" in detail
    assert "PRIVATE_REJECTED_FORMAT_VALUE" not in detail + "\n".join(events)


@pytest.mark.parametrize("change", [
    {"commands": ["sort"]},
    {"permissions": {"overwrite": True}},
    {"action": "tools", "commands": ["sort"]},
])
def test_valid_recipe_does_not_bypass_invalid_planner_envelope(change, table, monkeypatch):
    envelope = final(shell_document())
    envelope.update(change)
    state = scripted_inference(monkeypatch, [envelope])
    with pytest.raises(GenerationError, match="exhausted 1.*Invalid planner"):
        SheetbendCompiler(max_attempts=1).generate(
            Request.bind("sort", input_paths=[table], output_path="-"), [],
        )
    assert len(state["prompts"]) == 1


def test_python_fallback_fixture_structure_can_be_repaired(recipe, table, monkeypatch):
    correct = python_document(recipe)
    invalid = deepcopy(correct)
    invalid["tests"][0]["files"][0]["path"] = "wrong.csv"
    state = scripted_inference(monkeypatch, [final(invalid), final(correct)])
    result = SheetbendCompiler().generate(
        Request.bind("filter CSV", input_paths=[table], output_path="-"), [],
    )
    assert not result.recipe.is_shell and result.recipe.fallback_reason
    assert len(state["prompts"]) == 2
    assert "empty relative path" in state["prompts"][1][0]


def test_changed_input_aborts_instead_of_replanning(table, monkeypatch):
    request = Request.bind("sort", input_paths=[table], output_path="-")

    def change_input():
        table.write_text("different data\n")
        return final(shell_document())

    state = scripted_inference(monkeypatch, [change_input])
    with pytest.raises(ContractError, match="changed after binding"):
        SheetbendCompiler().generate(request, [])
    assert len(state["prompts"]) == 1 and not state["open"]


def test_exhausted_lookup_budget_reports_no_execution(table, monkeypatch):
    state = scripted_inference(monkeypatch, [
        {"action": "tools", "commands": ["jq"], "recipe": None},
        {"action": "tools", "commands": ["mlr"], "recipe": None},
    ])
    with pytest.raises(GenerationError, match="exhausted 2.*did not submit a final"):
        SheetbendCompiler(max_attempts=2).generate(
            Request.bind("sort", input_paths=[table], output_path="-"), [],
        )
    assert len(state["prompts"]) == 2 and not state["open"]


def test_transport_error_never_retries_and_body_requires_high_debug(table, monkeypatch):
    class RejectedRequest(Exception):
        status_code = 400
        body = {"error": {
            "message": "Unsupported response_format; api_key='SUPER_SECRET'",
            "param": "response_format", "type": "invalid_request_error",
        }}

    for debug_level in (0, 2, 3):
        error = RejectedRequest("PRIVATE_PROVIDER_RESPONSE")
        state = scripted_inference(monkeypatch, [error])
        events = []
        with pytest.raises(GenerationError) as caught:
            SheetbendCompiler(
                allow_institutional=True, max_attempts=4,
                debug_level=debug_level, on_event=events.append,
            ).generate(Request.bind("sort", input_paths=[table], output_path="-"), [])
        message = str(caught.value)
        log = "\n".join(events)
        assert caught.value.__cause__ is error
        assert "HTTP 400" in message and "structured request" in message
        assert "PRIVATE_PROVIDER_RESPONSE" not in message + log
        assert "SUPER_SECRET" not in message + log
        assert ("Unsupported response_format" in log) is (debug_level == 3)
        assert len(state["prompts"]) == 1 and len(state["connections"]) == 1
        assert not state["open"]


def test_normal_generation_is_quiet_and_diagnostics_do_not_change_cache(table, monkeypatch):
    scripted_inference(monkeypatch, [final(shell_document())])
    events = []
    compiler = SheetbendCompiler(on_event=events.append)
    compiler.generate(Request.bind("sort", input_paths=[table], output_path="-"), [])
    assert events == []
    assert compiler.cache_identity() == SheetbendCompiler(debug_level=3).cache_identity()


@pytest.mark.parametrize("value", [0, 9, -1, True, 2.5])
def test_attempt_budget_is_bounded(value):
    with pytest.raises(ValueError, match="max_attempts"):
        SheetbendCompiler(max_attempts=value)


def test_diagnostic_redaction_does_not_serialize_request_objects():
    message = bounded_visible(
        'api_key="SECRET" token=OTHER Authorization: Bearer ABCDEF '
        "https://user:PASS@example.org/v1?api_key=URL_SECRET \x1b[31m"
    )
    assert all(value not in message for value in ("SECRET", "OTHER", "ABCDEF", "PASS", "\x1b"))
    assert "example.org" in message and "\\x1b" in message
    cause = RuntimeError("PRIVATE")
    outer = RuntimeError("MORE_PRIVATE")
    outer.__cause__ = cause
    assert safe_exception_detail(outer) == "RuntimeError <- RuntimeError"


def test_local_wins_and_response_is_consumed_in_context(recipe, table, tmp_path, monkeypatch):
    pytest.importorskip("sheetbend", reason="Sheetbend dependency is not installed")
    from sheetbend.source import Source

    document = python_document(recipe)
    seen = []
    state = {"open": False}

    @contextmanager
    def connect(self, model, **kwargs):
        seen.append((self.name, model, kwargs))
        state["open"] = True

        def prompt(text, **options):
            assert options["stream"] is False
            assert options["schema"]["properties"]["action"]["enum"] == ["tools", "final"]
            assert "REQUEST DATA" in text

            def consume():
                assert state["open"]
                return json.dumps(final(document))

            return SimpleNamespace(text=consume)

        try:
            yield SimpleNamespace(prompt=prompt)
        finally:
            state["open"] = False

    monkeypatch.setattr(Source, "_connect", connect)
    compiler = SheetbendCompiler(config_path=config_file(tmp_path), allow_institutional=True, reasoning="off")
    result = compiler.generate(Request.bind("filter", input_paths=[table], output_path="-"), [])
    assert seen[0][0] == "laptop"
    assert seen[0][2] == {"reasoning": "off", "application": "any", "tool": "compile"}
    assert result.provenance["scope"] == "local" and result.recipe.digest == Recipe(document).digest
    assert not state["open"]


def test_nonlocal_requires_explicit_permission(table, tmp_path, monkeypatch):
    pytest.importorskip("sheetbend", reason="Sheetbend dependency is not installed")
    from sheetbend.source import Source

    path = config_file(tmp_path, local=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Disallowed source must not connect")

    monkeypatch.setattr(Source, "_connect", forbidden)
    compiler = SheetbendCompiler(config_path=path)
    with pytest.raises(GenerationError, match="No configured"):
        compiler.generate(Request.bind("filter", input_paths=[table], output_path="-"), [])


def test_model_failure_never_falls_back_or_exposes_provider_body(table, tmp_path, monkeypatch):
    pytest.importorskip("sheetbend", reason="Sheetbend dependency is not installed")
    from sheetbend.source import Source

    seen = []

    @contextmanager
    def connect(self, model, **kwargs):
        seen.append(self.name)
        raise RuntimeError("PRIVATE_PROVIDER_RESPONSE")
        yield

    monkeypatch.setattr(Source, "_connect", connect)
    compiler = SheetbendCompiler(config_path=config_file(tmp_path), allow_institutional=True)
    with pytest.raises(GenerationError) as caught:
        compiler.generate(Request.bind("filter", input_paths=[table], output_path="-"), [])
    assert seen == ["laptop"] and "PRIVATE_PROVIDER_RESPONSE" not in str(caught.value)
