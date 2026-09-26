"""Reusable public workflow stages shared by the CLI and Python callers."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import time
from typing import Any, BinaryIO

from .compiler import SheetbendCompiler
from .contracts import canonical_json, validate_document
from .errors import CacheError, ContractError
from .inspection import inspect_inputs
from .output import publish_output, validate_output
from .recipe import Recipe
from .recipe_store import RecipeStore
from .request import Request
from .sandbox import Sandbox
from .testing import SUFFIXES, TestReport, check_recipe


@dataclass(frozen=True, slots=True)
class Plan:
    """A recipe plus fresh caller authority. Construct through Engine.prepare()."""

    request: Request
    recipe: Recipe
    cache_hit: bool

    @classmethod
    def bind(cls, request: Request, recipe: Recipe) -> "Plan":
        """Bind an explicitly supplied library/file recipe, without generation or cache lookup."""
        request.recheck()
        recipe.validate_inputs(request.inputs)
        recipe.bind_parameters(request.parameters)
        return cls(request, recipe, False)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": 1, "recipe_digest": self.recipe.digest,
            "description": self.recipe.description,
            "inputs": [item.to_dict() for item in self.request.inputs],
            "output_path": None if self.request.output_path is None else str(self.request.output_path),
            "overwrite": self.request.overwrite,
            "modify_path": None if self.request.modify_path is None else str(self.request.modify_path),
            "parameters": self.recipe.bind_parameters(self.request.parameters),
            "as_of": self.request.as_of, "cache_hit": self.cache_hit, "execution_performed": False,
        }
        validate_document(data, "plan")
        return data

    def preview(self) -> str:
        """Show the actual program and JSON binding contract; do not execute fixture code."""
        import json
        return (
            "# Bound plan (program and fixture execution NOT performed)\n"
            + json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
            + "\n\n# Worker program: run(inputs, output, params, context)\n"
            + "# inputs/output are bound by any; output is a staged file, not the host destination.\n"
            + self.recipe.code.rstrip() + "\n"
        )

    def __repr__(self) -> str:
        return f"Plan(recipe={self.recipe.digest[:12]!r}, cache_hit={self.cache_hit}, output={self.request.output_path!r})"


class Engine:
    """Prepare, check, and execute the same library workflow used by the CLI.

    Passing store=None disables recipe persistence. Compiler construction is lazy
    with respect to configuration, credentials, imports, and network activity.
    """

    def __init__(
        self, *, compiler: SheetbendCompiler | None = None, store: RecipeStore | None = None,
        sandbox: Sandbox | None = None, sample_bytes: int = 4096,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        if type(sample_bytes) is not int or not 0 <= sample_bytes <= 16384:
            raise ValueError("sample_bytes must be between 0 and 16384.")
        self.compiler = compiler if compiler is not None else SheetbendCompiler()
        self.store = store
        self.sandbox = sandbox if sandbox is not None else Sandbox()
        self.sample_bytes = sample_bytes
        self.on_event = on_event

    def _emit(self, message: str) -> None:
        if self.on_event is not None:
            self.on_event(message)

    def prepare(self, request: Request, *, recipe_name: str | None = None, regenerate: bool = False) -> Plan:
        """Bind a literal saved recipe or generate once. This never executes a program."""
        request.recheck()
        if recipe_name is not None:
            if request.task is not None or regenerate:
                raise ContractError("Use a task or a saved recipe, not both; saved recipes are immutable.")
            if self.store is None:
                raise CacheError("Saved recipe selection requires a RecipeStore.")
            recipe, cached = self.store.get(recipe_name), True
        else:
            if request.task is None:
                raise ContractError("Supply a task or --recipe.")
            key = sha256(canonical_json({
                "contract": "python-stdlib-v1", "task": request.task,
                "inputs": [str(item.path) for item in request.inputs],
                "output_hint": None if request.output_path is None else request.output_path.suffixes,
                "compiler": self.compiler.cache_identity(), "sample_bytes": self.sample_bytes,
            })).hexdigest()
            recipe = None if self.store is None or regenerate else self.store.find(key)
            cached = recipe is not None
            if recipe is None:
                inspected = inspect_inputs(request.inputs, sample_bytes=self.sample_bytes)
                generated = self.compiler.generate(request, inspected)
                recipe = generated.recipe
                recipe.validate_inputs(request.inputs)
                recipe.bind_parameters(request.parameters)
                if self.store is not None:
                    self.store.put(recipe, provenance=generated.provenance)
                    winner = self.store.remember(key, recipe.digest, replace=regenerate)
                    if winner != recipe.digest:
                        recipe, cached = self.store.get(winner), True
        recipe.validate_inputs(request.inputs)
        recipe.bind_parameters(request.parameters)
        self._emit(f"{'Using cached' if cached else 'Prepared'} recipe {recipe.digest[:12]}.")
        return Plan(request, recipe, cached)

    def check(self, plan: Plan, *, use_cache: bool = False) -> TestReport:
        """Run synthetic fixtures only. A direct call checks afresh unless requested otherwise."""
        plan.request.recheck()
        plan.recipe.validate_inputs(plan.request.inputs)
        self.sandbox.require_available()
        runtime = self.sandbox.fingerprint()
        count = None if self.store is None or not use_cache else self.store.qualification(plan.recipe, runtime)
        if count is not None and count == len(plan.recipe.to_dict()["tests"]):
            return TestReport(plan.recipe.digest, count, cache_hit=True)
        self._emit("Checking synthetic fixtures.")
        report = check_recipe(plan.recipe, self.sandbox)
        if self.store is not None:
            self.store.qualify(plan.recipe, runtime, report.passed)
        return report

    def execute(self, plan: Plan, *, stdout: BinaryIO | None = None) -> dict[str, Any]:
        """Qualify, execute, validate, then publish exactly one result; never regenerate on failure."""
        if (plan.request.output_path is None) != (stdout is not None):
            raise ValueError("Provide a binary stdout stream exactly when output_path is '-'.")
        plan.request.recheck()
        report = self.check(plan, use_cache=True)
        document = plan.recipe.to_dict()
        parameters = plan.recipe.bind_parameters(plan.request.parameters)
        started = datetime.now(timezone.utc).isoformat()
        tick = time.monotonic()
        self._emit(plan.recipe.description)
        with self.sandbox.execute(
            plan.recipe.code, inputs=[item.path for item in plan.request.inputs], parameters=parameters,
            context=plan.request.context(), output_suffix=SUFFIXES[document["output"]["format"]],
        ) as staged:
            digest, size = validate_output(staged.path, document["output"], max_bytes=self.sandbox.limits.output_bytes)
            record = {
                "schema_version": 1, "recipe_digest": plan.recipe.digest, "started_at": started,
                "elapsed_seconds": time.monotonic() - tick, "as_of": plan.request.as_of,
                "parameters": parameters, "inputs": [item.to_dict() for item in plan.request.inputs],
                "output_path": None if plan.request.output_path is None else str(plan.request.output_path),
                "output_sha256": digest, "output_bytes": size, "tests_passed": report.passed,
                "fixture_cache_hit": report.cache_hit, "backend": staged.backend,
                "runtime_fingerprint": self.sandbox.fingerprint(), "published": False,
            }
            validate_document(record, "run")
            publish_output(staged.path, plan.request, stdout=stdout)
            record["published"] = True
        if self.store is not None:
            try:
                self.store.record_run(record)
            except CacheError as exc:
                raise CacheError("The output WAS published, but storing its run receipt failed. Do not assume rollback.") from exc
        self._emit(f"Published {size:,} bytes.")
        return record

    def __repr__(self) -> str:
        return f"Engine(cache={self.store!r}, sandbox={self.sandbox!r})"
