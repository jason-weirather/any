import json
from pathlib import Path
import tomllib

from jsonschema import Draft202012Validator
import pytest

from sheetbend_any import Recipe, Request, __version__
from sheetbend_any.contracts import load_schema
from sheetbend_any.errors import ContractError


def test_packaged_schemas_and_single_authored_version(recipe):
    for name in ("recipe", "plan", "run"):
        Draft202012Validator.check_schema(load_schema(name))
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["version"] == __version__
    assert recipe.digest == Recipe.from_json(json.dumps(recipe.to_dict())).digest


def test_recipe_owns_data_and_parameter_overrides(recipe):
    data = recipe.to_dict()
    other = Recipe(data)
    data["parameters"]["threshold"]["default"] = 100
    assert other.bind_parameters({}) == {"threshold": 20}
    assert other.bind_parameters({"threshold": 35}) == {"threshold": 35}
    assert other.digest == recipe.digest
    with pytest.raises(ContractError):
        other.bind_parameters({"threshold": "35"})
    with pytest.raises(ContractError):
        other.bind_parameters({"typo": 35})
    with pytest.raises(ContractError):
        other.bind_parameters({"threshold": float("nan")})


@pytest.mark.parametrize("update", [
    {"code": "not python ["}, {"code": "print('no entrypoint')"},
    {"code": "def run(*args): pass"},
    {"code": "import pandas\ndef run(a,b,c,d): pass"},
    {"tests": []}, {"permissions": {"overwrite": True}},
    {"runtime": "bash"},
])
def test_bad_recipes_fail_without_execution(recipe, update):
    data = recipe.to_dict()
    data.update(update)
    with pytest.raises(ContractError):
        Recipe(data)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../escape", "a//b", "a\\b", "", "a/./b"])
def test_fixture_traversal_is_rejected(recipe, path):
    data = recipe.to_dict()
    data["inputs"] = [{"kind": "directory", "format": "directory", "required_columns": []}]
    data["tests"][0]["files"][0]["path"] = path
    with pytest.raises(ContractError):
        Recipe(data)


def test_duplicate_json_keys_and_nonfinite_values_rejected():
    for value in ('{"schema_version":1,"schema_version":2}', '{"x":NaN}', '```json\n{}\n```'):
        with pytest.raises(ContractError):
            Recipe.from_json(value)


def test_required_input_columns_are_checked_on_replay(recipe, table):
    request = Request.bind(None, input_paths=[table], output_path="-")
    recipe.validate_inputs(request.inputs)
    table.write_text("cell_id,CD8\n1,2\n")
    changed = Request.bind(None, input_paths=[table], output_path="-")
    with pytest.raises(ContractError, match="Missing required"):
        recipe.validate_inputs(changed.inputs)


def test_input_order_and_types_are_explicit(recipe, table, tmp_path):
    with pytest.raises(ContractError, match="requires 1"):
        recipe.validate_inputs(())
    request = Request.bind(None, input_paths=[tmp_path], output_path="-")
    with pytest.raises(ContractError, match="kind"):
        recipe.validate_inputs(request.inputs)


def test_parameter_type_is_schema_owned(recipe):
    data = recipe.to_dict()
    data["parameters"]["threshold"]["type"] = "string"
    with pytest.raises(ContractError, match="Parameter"):
        Recipe(data)


def test_source_code_is_not_evaluated_during_validation(recipe, tmp_path):
    marker = tmp_path / "must-not-exist"
    data = recipe.to_dict()
    data["code"] = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\ndef run(a,b,c,d): pass"
    Recipe(data)
    assert not marker.exists()


def test_header_read_does_not_reject_a_split_utf8_tail(table, recipe):
    table.write_text("cell_id,CD3\n" + "001,22\n" * 10000 + "é")
    request = Request.bind(None, input_paths=[table], output_path="-")
    recipe.validate_inputs(request.inputs)


def test_fixture_timestamp_must_be_aware(recipe):
    data = recipe.to_dict()
    data["tests"][0]["files"][0]["modified_at"] = "2026-01-01"
    with pytest.raises(ContractError, match="offset"):
        Recipe(data)
