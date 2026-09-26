"""Small fixtures and explicitly labeled orchestration doubles, never a production backend."""

from contextlib import contextmanager
import csv
from pathlib import Path
import tempfile

import pytest

from sheetbend_any import Recipe
from sheetbend_any.compiler import GeneratedRecipe
from sheetbend_any.errors import ExecutionError
from sheetbend_any.sandbox import Limits, StagedResult


@pytest.fixture
def recipe():
    return Recipe.from_json((Path(__file__).parents[1] / "examples/filter-cd3.recipe.json").read_bytes())


@pytest.fixture
def table(tmp_path):
    path = tmp_path / "cells.csv"
    path.write_text("cell_id,CD3\n001,5\n002,22\n003,40\n")
    return path


class CompilerDouble:
    """Return one authored fixture recipe. This is not an inference test."""

    def __init__(self, recipe):
        self.recipe = recipe
        self.calls = 0

    def cache_identity(self):
        return {"double": True}

    def generate(self, request, inspected):
        self.calls += 1
        return GeneratedRecipe(self.recipe, {"test_double": True})


class SandboxDouble:
    """An authored CSV operation, not eval/exec and NOT a security or recipe-code test."""

    def __init__(self):
        self.calls = 0
        self.limits = Limits()
        self.temporary_directory = None
        self.fail = False

    def require_available(self):
        pass

    def fingerprint(self):
        return "orchestration-double"

    @contextmanager
    def execute(self, code, *, inputs, parameters, context, output_suffix):
        self.calls += 1
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.csv"
            if self.fail:
                raise ExecutionError("Deliberate failure", returncode=1)
            try:
                with inputs[0].open(newline="") as source, output.open("w", newline="") as target:
                    reader = csv.DictReader(source)
                    writer = csv.DictWriter(target, fieldnames=reader.fieldnames, lineterminator="\n")
                    writer.writeheader()
                    for row in reader:
                        if float(row["CD3"]) > parameters["threshold"]:
                            writer.writerow(row)
            except ValueError as exc:
                raise ExecutionError("Invalid numeric value", returncode=1) from exc
            yield StagedResult(output, b"", b"", 0.01, "ORCHESTRATION-DOUBLE-NOT-SANDBOXED")


@pytest.fixture
def compiler(recipe):
    return CompilerDouble(recipe)


@pytest.fixture
def sandbox_double():
    return SandboxDouble()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-base"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-base"))
    monkeypatch.setenv("SHEETBEND_RUNTIME_DIR", str(tmp_path / "sheetbend-runtime"))
    monkeypatch.delenv("SHEETBEND_CONFIG", raising=False)


@pytest.fixture
def cli_runner():
    """Click 8.1 and 8.2+ have different capture constructors; both are declared supported."""
    import inspect
    from click.testing import CliRunner
    return CliRunner(**({"mix_stderr": False} if "mix_stderr" in inspect.signature(CliRunner).parameters else {}))
