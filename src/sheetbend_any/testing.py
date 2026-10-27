"""Declarative synthetic fixtures use exactly the production execution boundary."""

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import tempfile

from .errors import ContractError, ExecutionError
from .output import validate_output
from .recipe import Recipe
from .sandbox import Sandbox

FIXTURE_NOW = "2000-01-01T00:00:00+00:00"
SUFFIXES = {"csv": ".csv", "tsv": ".tsv", "json": ".json", "text": ".txt", "bytes": ".bin"}


@dataclass(frozen=True, slots=True)
class TestReport:
    """Evidence from fixtures, not semantic certification or a sandbox audit."""

    recipe_digest: str
    passed: int
    cache_hit: bool = False


def check_recipe(recipe: Recipe, sandbox: Sandbox, *, inventory: dict | None = None) -> TestReport:
    """Run every fixture, failing at the first unexpected error or byte mismatch."""
    document = recipe.to_dict()
    sandbox.require_available()
    for case in document["tests"]:
        with tempfile.TemporaryDirectory(prefix="any-fixture-", dir=sandbox.temporary_directory) as temporary:
            root = Path(temporary).resolve()
            paths = []
            for index, requirement in enumerate(document["inputs"]):
                name = f"input{index}" + (SUFFIXES[requirement["format"]] if requirement["kind"] == "file" else "")
                path = root / name
                if requirement["kind"] == "directory":
                    path.mkdir()
                paths.append(path)
            for file in case["files"]:
                path = paths[file["input_index"]]
                if file["path"]:
                    path = path / file["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                # Conflicting ancestor/descendant fixture paths fail, never overwrite.
                with path.open("xb") as handle:
                    handle.write(file["content"].encode("utf-8"))
                timestamp = datetime.fromisoformat(file.get("modified_at") or FIXTURE_NOW).timestamp()
                os.utime(path, (timestamp, timestamp))
            expected = case["expected_output"]
            try:
                execute = sandbox.execute_shell if recipe.is_shell else sandbox.execute
                payload = recipe.pipeline if recipe.is_shell else recipe.code
                with execute(
                    payload, inputs=paths, parameters=recipe.bind_parameters(case["parameters"]),
                    context={"now": FIXTURE_NOW, "input_names": [path.name for path in paths],
                             "any_phase": "fixture"},
                    output_suffix=SUFFIXES[document["output"]["format"]],
                    **({"inventory": inventory} if recipe.is_shell else {}),
                ) as result:
                    if expected is None:
                        raise ContractError(f"Fixture {case['name']!r}: expected a program error, but it succeeded.")
                    validate_output(result.path, document["output"], max_bytes=sandbox.limits.output_bytes)
                    expected_bytes = expected.encode("utf-8")
                    with result.path.open("rb") as handle:
                        actual = handle.read(len(expected_bytes) + 1)
                    if actual != expected_bytes:
                        raise ContractError(f"Fixture {case['name']!r}: output does not match the expected bytes.")
            except ExecutionError as exc:
                if expected is not None or exc.returncode != 1:
                    raise ContractError(f"Fixture {case['name']!r}: confined execution failed: {exc}") from exc
    return TestReport(recipe.digest, len(document["tests"]))
