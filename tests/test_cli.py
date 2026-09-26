import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from sheetbend_any import RecipeStore, __version__
from sheetbend_any.cli import main


def test_version_and_help_are_eager_and_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("SHEETBEND_CONFIG", str(tmp_path / "missing.toml"))
    assert CliRunner().invoke(main, ["--version"]).output.strip() == __version__
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0 and "--dry-run" in result.output
    assert "--no-overwrite" not in result.output
    assert not (tmp_path / "cache-base").exists()


def test_output_option_is_required():
    result = CliRunner().invoke(main, ["list files"])
    assert result.exit_code == 2 and "output" in result.output


def test_dry_run_from_recipe_file_never_needs_sandbox_or_sheetbend(table, tmp_path, monkeypatch, cli_runner):
    from sheetbend_any.sandbox import Sandbox
    def forbidden(*args, **kwargs):
        pytest.fail("Dry-run must not execute or probe a sandbox")
    monkeypatch.setattr(Sandbox, "execute", forbidden)
    monkeypatch.setattr(Sandbox, "require_available", forbidden)
    path = Path(__file__).parents[1] / "examples/filter-cd3.recipe.json"
    out = tmp_path / "out.csv"
    result = cli_runner.invoke(main, ["--recipe-file", str(path), "-i", str(table), "-o", str(out), "--dry-run", "--no-cache"])
    assert result.exit_code == 0, result.output
    assert "def run" in result.stdout and "execution_performed" in result.stdout
    assert result.stderr == "" and not out.exists()
    assert not (tmp_path / "cache-base").exists()


def test_overwrite_refused_before_any_model_work(table, tmp_path, monkeypatch, cli_runner):
    from sheetbend_any.compiler import SheetbendCompiler
    def forbidden(*args, **kwargs):
        pytest.fail("Existing output must be rejected before model generation")
    monkeypatch.setattr(SheetbendCompiler, "generate", forbidden)
    out = tmp_path / "out.csv"
    out.write_text("keep")
    result = cli_runner.invoke(main, ["filter", "-i", str(table), "-o", str(out)])
    assert result.exit_code == 1 and "already exists" in result.stderr
    assert result.stdout == "" and out.read_text() == "keep"
    assert not (tmp_path / "cache-base").exists()


def test_saved_recipe_listing_is_offline(recipe, tmp_path):
    store = RecipeStore(tmp_path / "cache")
    store.save_name("cd3", recipe)
    result = CliRunner().invoke(main, ["--cache-directory", str(store.directory), "--list-recipes"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["digest"] == recipe.digest


@pytest.mark.parametrize("options", [
    ["task", "--recipe", "name", "-o", "-"],
    ["task", "-o", "-", "--dry-run", "--check"],
    ["task", "-o", "-", "--no-cache", "--save-recipe", "name"],
    ["--doctor", "task"], ["--doctor", "--list-recipes"],
    ["--recipe", "name", "-o", "-", "--regenerate"],
    ["--recipe", "name", "-o", "-", "--source", "model"],
])
def test_conflicting_modes_fail_before_work(options):
    assert CliRunner().invoke(main, options).exit_code == 2
