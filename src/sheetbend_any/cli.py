"""A Click front door over the same prepare/check/execute library stages."""

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import click

from ._version import __version__
from .compiler import SheetbendCompiler
from .engine import Engine, Plan
from .errors import AnyError
from .recipe import Recipe
from .recipe_store import RecipeStore
from .request import Request
from .sandbox import Limits, Sandbox

_DEFAULTS = Limits()


def _visible(text: str) -> str:
    """Diagnostics/code previews cannot inject ANSI control sequences into a terminal."""
    return "".join(char if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
                   else f"\\x{ord(char):02x}" for char in text)


def _parameters(values: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    result = {}
    for name, text in values:
        if name in result:
            raise click.UsageError(f"Repeated parameter: {name}")
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        result[name] = value
    return result


@click.command(context_settings={"show_default": True})
@click.version_option(__version__, message="%(version)s")
@click.argument("task", required=False)
@click.option("--input-path", "-i", "input_paths", multiple=True, type=click.Path(path_type=Path), help="Read-only input file/directory. Repeat in the required order.")
@click.option("--output-path", "-o", help="Required for a task: one output file, or '-' for stdout.")
@click.option("--overwrite", is_flag=True, help="Permit replacement of the exact output; never grants input modification.")
@click.option("--modify", "modify_path", type=click.Path(path_type=Path), help="Permit staged replacement of this exact input/output file.")
@click.option("--dry-run", is_flag=True, help="Show the actual program and binding contract. Execute neither program nor fixtures.")
@click.option("--check", "check_only", is_flag=True, help="Execute synthetic fixtures only; do not run on the real inputs or publish output.")
@click.option("--recipe", "recipe_name", help="Use an immutable cached name or full SHA-256 digest, without inference.")
@click.option("--recipe-file", type=click.Path(path_type=Path, exists=True, dir_okay=False), help="Read a schema-valid recipe JSON file, without inference.")
@click.option("--save-recipe", help="Save this program under a new literal name; never replace another named program.")
@click.option("--regenerate", is_flag=True, help="Explicitly generate a new recipe for a task instead of using its cache entry.")
@click.option("--param", "parameters", nargs=2, multiple=True, metavar="NAME VALUE", help="Override a declared parameter. JSON scalars are parsed; other values are strings.")
@click.option("--as-of", help="Bind context['now'] to an aware ISO-8601 timestamp; otherwise use this invocation's time.")
@click.option("--source", help="Explicit Sheetbend source, still constrained by allowed scopes.")
@click.option("--model", help="Explicit configured model at the selected Sheetbend source.")
@click.option("--allow-institutional", is_flag=True, help="Permit institutional processing after local sources in selection priority.")
@click.option("--allow-external", is_flag=True, help="Permit external processing after other allowed scopes. Never retries across scopes.")
@click.option("--reasoning", type=click.Choice(["provider", "off", "on", "low", "medium", "high"]), default=None, show_default="Sheetbend model configuration")
@click.option("--config-path", type=click.Path(path_type=Path), help="Select one Sheetbend configuration; any has no second endpoint registry.")
@click.option("--sample-bytes", type=click.IntRange(0, 16384), default=4096, help="Maximum file preview bytes; zero is metadata-only. Global preview budget is 16 KiB.")
@click.option("--cache-directory", type=click.Path(path_type=Path), default=None, show_default="XDG_CACHE_HOME/any or ~/.cache/any")
@click.option("--no-cache", is_flag=True, help="Disable any recipe-cache reads/writes and run receipts. Sheetbend owns its separate runtime ledger.")
@click.option("--temporary-directory", type=click.Path(path_type=Path, exists=True, file_okay=False), help="Existing parent for automatically cleaned private workspaces.")
@click.option("--timeout-seconds", type=click.FloatRange(min=0, min_open=True), default=_DEFAULTS.timeout_seconds, help="Worker wall-clock/CPU budget. Sheetbend owns generation timeouts.")
@click.option("--memory-mib", type=click.IntRange(min=1), default=_DEFAULTS.memory_bytes // 1048576)
@click.option("--output-mib", type=click.IntRange(min=1), default=_DEFAULTS.output_bytes // 1048576)
@click.option("--scratch-mib", type=click.IntRange(min=1), default=_DEFAULTS.scratch_bytes // 1048576)
@click.option("--doctor", is_flag=True, help="Run synthetic OS-boundary checks without contacting a model.")
@click.option("--list-recipes", is_flag=True, help="List cached recipe identifiers/names as JSON, without inference or execution.")
@click.option("--verbose", "-v", count=True, help="-v: useful execution stages on stderr. -vv: include the bound plan and actual code.")
def main(
    task: str | None, input_paths: tuple[Path, ...], output_path: str | None,
    overwrite: bool, modify_path: Path | None, dry_run: bool, check_only: bool,
    recipe_name: str | None, recipe_file: Path | None, save_recipe: str | None, regenerate: bool,
    parameters: tuple[tuple[str, str], ...], as_of: str | None, source: str | None,
    model: str | None, allow_institutional: bool, allow_external: bool, reasoning: str | None,
    config_path: Path | None, sample_bytes: int, cache_directory: Path | None,
    no_cache: bool, temporary_directory: Path | None, timeout_seconds: float,
    memory_mib: int, output_mib: int, scratch_mib: int, doctor: bool, list_recipes: bool,
    verbose: int,
) -> None:
    """Manufacture a small utility from TASK, then test, confine, and run it.

    Python 3.13+ standard-library programs only. Inputs are read-only. A new output
    is created by default; replacement always requires explicit permission.
    No sudo, dependency installation, raw shell execution, or unrestricted fallback.
    """
    try:
        if doctor or list_recipes:
            if doctor and list_recipes:
                raise click.UsageError("Use --doctor or --list-recipes, not both.")
            if any((task, input_paths, output_path, recipe_name, recipe_file, dry_run, check_only,
                    overwrite, modify_path, save_recipe, regenerate, parameters, as_of)):
                raise click.UsageError("Inspection modes cannot also perform a task.")
            if list_recipes:
                if no_cache:
                    raise click.UsageError("--list-recipes requires cache access.")
                click.echo(json.dumps(RecipeStore(cache_directory).entries(), indent=2, ensure_ascii=False))
                return
        else:
            if sum(value is not None for value in (task, recipe_name, recipe_file)) != 1:
                raise click.UsageError("Supply exactly one TASK, --recipe, or --recipe-file.")
            if output_path is None:
                raise click.UsageError("An explicit --output-path / -o is required; use -o - for stdout.")
        if dry_run and check_only:
            raise click.UsageError("--dry-run does not execute tests; use --check separately.")
        if regenerate and task is None:
            raise click.UsageError("--regenerate requires a task, not a saved recipe.")
        if no_cache and (save_recipe or recipe_name or cache_directory):
            raise click.UsageError("--no-cache cannot be combined with named recipe/cache operations.")
        if (recipe_name or recipe_file) and any((source, model, config_path, reasoning, allow_institutional, allow_external)):
            raise click.UsageError("Model/source options do not apply to a saved recipe.")
        limits = Limits(timeout_seconds, memory_mib * 1048576, output_mib * 1048576, scratch_mib * 1048576)
        sandbox = Sandbox(limits=limits, temporary_directory=temporary_directory)
        if doctor:
            click.echo(json.dumps(sandbox.doctor(), indent=2))
            return
        # Permission failures happen before cache creation, data inspection, or generation.
        request = Request.bind(
            task, input_paths=input_paths, output_path=output_path, overwrite=overwrite,
            modify_path=modify_path, parameters=_parameters(parameters), as_of=as_of,
        )
        store = None if no_cache else RecipeStore(cache_directory)
        if save_recipe and any(entry["name"] == save_recipe for entry in store.entries()):
            raise click.UsageError("That saved recipe name already exists; use --recipe or a new name.")
        # Fail cheaply when execution is impossible; preview does not need a sandbox.
        if not dry_run:
            sandbox.require_available()
        compiler = SheetbendCompiler(
            config_path=config_path, source=source, model=model,
            allow_institutional=allow_institutional, allow_external=allow_external, reasoning=reasoning,
        )
        def event(message: str) -> None:
            click.echo(_visible(message), err=True)
        engine = Engine(
            compiler=compiler, store=store, sandbox=sandbox, sample_bytes=sample_bytes,
            on_event=event if verbose else None,
        )
        if recipe_file is None:
            plan = engine.prepare(request, recipe_name=recipe_name, regenerate=regenerate)
        else:
            with recipe_file.open("rb") as handle:
                recipe = Recipe.from_json(handle.read(262145))
            plan = Plan.bind(request, recipe)
        if dry_run:
            click.echo(_visible(plan.preview()), nl=False)
        else:
            if verbose > 1:
                click.echo(_visible(plan.preview()), err=True, nl=False)
            if check_only:
                click.echo(json.dumps(asdict(engine.check(plan)), indent=2))
            else:
                engine.execute(plan, stdout=click.get_binary_stream("stdout") if request.output_path is None else None)
        if save_recipe:
            store.save_name(save_recipe, plan.recipe)
    except AnyError as exc:
        raise click.ClickException(_visible(str(exc))) from exc
    except (OSError, ValueError) as exc:
        raise click.ClickException(_visible(str(exc))) from exc
