import io
import sqlite3

import pytest

from sheetbend_any import Engine, RecipeStore, Request
from sheetbend_any.errors import CacheError, ContractError
from sheetbend_any.inspection import inspect_inputs


def test_cached_recipe_uses_current_data_without_another_model_call(compiler, sandbox_double, table, tmp_path):
    store = RecipeStore(tmp_path / "cache")
    engine = Engine(compiler=compiler, store=store, sandbox=sandbox_double)
    first = engine.prepare(Request.bind("filter", input_paths=[table], output_path="-"))
    result = io.BytesIO()
    receipt = engine.execute(first, stdout=result)
    assert result.getvalue() == b"cell_id,CD3\n002,22\n003,40\n"
    assert receipt["published"] and not receipt["fixture_cache_hit"]
    assert compiler.calls == 1
    table.write_text("cell_id,CD3\n004,80\n005,2\n")
    second = engine.prepare(Request.bind("filter", input_paths=[table], output_path="-"))
    result = io.BytesIO()
    receipt = engine.execute(second, stdout=result)
    assert result.getvalue() == b"cell_id,CD3\n004,80\n"
    assert second.cache_hit and receipt["fixture_cache_hit"] and compiler.calls == 1
    assert len(store.runs()) == 2


def test_preview_is_not_execution_and_preserves_candidate(compiler, sandbox_double, table, tmp_path):
    engine = Engine(compiler=compiler, store=RecipeStore(tmp_path / "cache"), sandbox=sandbox_double)
    request = Request.bind("filter", input_paths=[table], output_path=tmp_path / "out.csv")
    plan = engine.prepare(request)
    text = plan.preview()
    assert "def run" in text and "NOT performed" in text
    assert sandbox_double.calls == 0 and not request.output_path.exists()
    replay = engine.prepare(request)
    assert replay.recipe.digest == plan.recipe.digest and compiler.calls == 1


def test_saved_recipe_never_remembers_overwrite_permission(recipe, table, tmp_path):
    store = RecipeStore(tmp_path / "cache")
    store.save_name("cd3", recipe)
    plan = Engine(store=store).prepare(Request.bind(None, input_paths=[table], output_path="-"), recipe_name="cd3")
    assert plan.cache_hit and not plan.request.overwrite and plan.request.modify_path is None
    assert store.get(recipe.digest).digest == recipe.digest


def test_failed_fixtures_leave_output_untouched(compiler, sandbox_double, table, tmp_path):
    engine = Engine(compiler=compiler, sandbox=sandbox_double)
    out = tmp_path / "out.csv"
    out.write_text("keep")
    plan = engine.prepare(Request.bind("filter", input_paths=[table], output_path=out, overwrite=True))
    sandbox_double.fail = True
    with pytest.raises(ContractError, match="Fixture"):
        engine.execute(plan)
    assert out.read_text() == "keep"


def test_check_only_never_runs_on_real_inputs(compiler, sandbox_double, table, tmp_path):
    engine = Engine(compiler=compiler, sandbox=sandbox_double)
    out = tmp_path / "out.csv"
    plan = engine.prepare(Request.bind("filter", input_paths=[table], output_path=out))
    report = engine.check(plan)
    assert report.passed == 2 and sandbox_double.calls == 2
    assert not out.exists()


def test_changed_columns_fail_without_regeneration(compiler, table, tmp_path):
    engine = Engine(compiler=compiler, store=RecipeStore(tmp_path / "cache"))
    engine.prepare(Request.bind("filter", input_paths=[table], output_path="-"))
    table.write_text("cell_id,CD8\n1,4\n")
    with pytest.raises(ContractError):
        engine.prepare(Request.bind("filter", input_paths=[table], output_path="-"))
    assert compiler.calls == 1


def test_store_detects_tampering_and_refuses_insecure_storage(recipe, tmp_path):
    store = RecipeStore(tmp_path / "cache")
    assert store.entries() == [] and not store.directory.exists()
    store.save_name("named", recipe)
    with sqlite3.connect(store.directory / "recipes.sqlite3") as db:
        db.execute("UPDATE recipes SET document=replace(document, 'Keep rows', 'Drop rows')")
    with pytest.raises(CacheError, match="digest"):
        store.get("named")
    store.directory.chmod(0o755)
    with pytest.raises(CacheError, match="private"):
        store.entries()


def test_store_eviction_protects_named_recipes(recipe, tmp_path):
    from sheetbend_any import Recipe
    store = RecipeStore(tmp_path / "cache", max_unnamed=1)
    store.save_name("keep", recipe)
    for text in ("another", "newest"):
        data = recipe.to_dict()
        data["description"] = text
        store.put(Recipe(data))
    assert store.get("keep").digest == recipe.digest
    assert len(store.entries()) == 2


def test_regeneration_is_explicit_and_named_recipes_are_immutable(compiler, recipe, table, tmp_path):
    from sheetbend_any import Recipe
    store = RecipeStore(tmp_path / "cache")
    engine = Engine(compiler=compiler, store=store)
    request = Request.bind("filter", input_paths=[table], output_path="-")
    first = engine.prepare(request)
    store.save_name("first", first.recipe)
    changed = recipe.to_dict()
    changed["description"] = "A deliberately new version"
    compiler.recipe = Recipe(changed)
    assert engine.prepare(request).recipe.digest == first.recipe.digest
    second = engine.prepare(request, regenerate=True)
    assert second.recipe.digest != first.recipe.digest
    assert store.get("first").digest == first.recipe.digest
    with pytest.raises(CacheError, match="already"):
        store.save_name("first", second.recipe)


def test_inspection_is_bounded_and_metadata_only_is_literal(table, tmp_path):
    request = Request.bind("inspect", input_paths=[table], output_path="-")
    result = inspect_inputs(request.inputs, sample_bytes=10)
    assert len(result[0]["preview"].encode()) <= 10
    assert "preview" not in inspect_inputs(request.inputs, sample_bytes=0)[0]
    directory = Request.bind("inspect", input_paths=[tmp_path], output_path="-")
    assert "entries" not in inspect_inputs(directory.inputs, sample_bytes=0)[0]


def test_relative_time_is_a_fresh_run_binding_not_a_recipe_constant(compiler, table, tmp_path):
    engine = Engine(compiler=compiler, store=RecipeStore(tmp_path / "cache"))
    first = engine.prepare(Request.bind("filter", input_paths=[table], output_path="-", as_of="2026-09-01T00:00:00-04:00"))
    second = engine.prepare(Request.bind("filter", input_paths=[table], output_path="-", as_of="2026-09-25T00:00:00-04:00"))
    assert first.recipe.digest == second.recipe.digest and compiler.calls == 1
    assert first.to_dict()["as_of"] != second.to_dict()["as_of"]
    with pytest.raises(ContractError, match="offset"):
        Request.bind("filter", output_path="-", as_of="2026-09-25T00:00:00")


def test_different_destinations_reuse_code_without_reusing_authority(compiler, table, tmp_path):
    engine = Engine(compiler=compiler, store=RecipeStore(tmp_path / "cache"))
    first = engine.prepare(Request.bind("filter", input_paths=[table], output_path=tmp_path / "first.csv"))
    second = engine.prepare(Request.bind("filter", input_paths=[table], output_path=tmp_path / "second.csv"))
    assert second.cache_hit and first.recipe.digest == second.recipe.digest
    assert compiler.calls == 1 and first.request.output_path != second.request.output_path


def test_global_inspection_budget(tmp_path):
    paths = []
    for index in range(10):
        path = tmp_path / f"input{index}.txt"
        path.write_text("x" * 10000)
        paths.append(path)
    request = Request.bind("inspect", input_paths=paths, output_path="-")
    observed = inspect_inputs(request.inputs, sample_bytes=4096)
    assert sum(len(row.get("preview", "").encode()) for row in observed) <= 16384
