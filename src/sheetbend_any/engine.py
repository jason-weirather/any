"""Reusable public workflow stages shared by the CLI and Python callers."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import time
from typing import Any, BinaryIO

from .compiler import SheetbendCompiler
from .command_plan import discover_commands, render_pipeline, resolve_pipeline
from .contracts import canonical_json, validate_document
from .errors import CacheError, ContractError
from .inspection import inspect_inputs
from .output import publish_output, validate_output
from .recipe import Recipe
from .recipe_store import RecipeStore
from .request import Request
from .sandbox import Sandbox
from .testing import SUFFIXES, TestReport, check_recipe


def _tool_bindings(recipe: Recipe) -> dict[str, Any]:
    if not recipe.is_shell:
        return {}
    names = ["bash", *(stage["argv"][0] for stage in recipe.pipeline)]
    inventory = discover_commands(names)
    for name, tool in inventory.items():
        if not tool["available"]:
            raise ContractError(f"Required command {name!r} is unavailable. Re-plan explicitly with --regenerate.")
        info = Path(tool["path"]).stat()
        tool["identity"] = [info.st_dev, info.st_ino, info.st_size,
                            info.st_mtime_ns, info.st_ctime_ns]
    return inventory


@dataclass(frozen=True, slots=True)
class Plan:
    """A recipe plus fresh caller authority. Construct through Engine.prepare()."""

    request: Request
    recipe: Recipe
    cache_hit: bool
    tools: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.tools is None:
            object.__setattr__(self, "tools", _tool_bindings(self.recipe))
        if self.recipe.is_shell:
            # Resolve parameter and input references during binding, before any run.
            self.command_preview()

    def recheck_tools(self) -> None:
        if self.recipe.is_shell and _tool_bindings(self.recipe) != self.tools:
            raise ContractError("Selected commands changed after planning; prepare a new plan.")

    def command_preview(self) -> str:
        """Host-bound computation; execution prints its actual sandbox path bindings."""
        if not self.recipe.is_shell:
            return self.recipe.code.rstrip() + "\n"
        argv = resolve_pipeline(
            self.recipe.pipeline, [item.path for item in self.request.inputs],
            self.recipe.bind_parameters(self.request.parameters), self.request.context(),
            inventory=self.tools,
        )
        return render_pipeline(argv)

    @classmethod
    def bind(cls, request: Request, recipe: Recipe) -> "Plan":
        """Bind an explicitly supplied library/file recipe, without generation or cache lookup."""
        request.recheck()
        recipe.validate_inputs(request.inputs)
        recipe.bind_parameters(request.parameters)
        return cls(request, recipe, False)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": 2, "recipe_digest": self.recipe.digest,
            "description": self.recipe.description,
            "inputs": [item.to_dict() for item in self.request.inputs],
            "output_path": None if self.request.output_path is None else str(self.request.output_path),
            "overwrite": self.request.overwrite,
            "modify_path": None if self.request.modify_path is None else str(self.request.modify_path),
            "parameters": self.recipe.bind_parameters(self.request.parameters),
            "as_of": self.request.as_of, "cache_hit": self.cache_hit, "execution_performed": False,
            "execution_kind": "shell" if self.recipe.is_shell else "python",
            "command": self.command_preview(), "tools": self.tools,
            "fallback_reason": self.recipe.fallback_reason,
        }
        validate_document(data, "plan")
        return data

    def preview(self) -> str:
        """Show the actual program and JSON binding contract; do not execute fixture code."""
        import json
        return (
            "# Bound plan (program and fixture execution NOT performed)\n"
            + json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
            + ("\n\n# Bash payload with host input bindings; execution prints actual sandbox paths.\n"
               if self.recipe.is_shell else "\n\n# Worker program: run(inputs, output, params, context)\n")
            + "# Output is staged; trusted publication handles the declared destination.\n"
            + self.command_preview()
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
        """Bind a saved recipe or prepare through bounded planning. Never run task code."""
        request.preflight(probe_output=False)
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
                "contract": "native-pipeline-v2", "task": request.task,
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

    def _runtime_fingerprint(self, plan: Plan) -> str:
        runtime = self.sandbox.fingerprint()
        if not plan.recipe.is_shell:
            return runtime
        return sha256(canonical_json({"sandbox": runtime, "tools": plan.tools})).hexdigest()

    def preflight(self, plan: Plan) -> dict[str, Any]:
        """Check current IO and the real boundary without running the candidate or fixtures."""
        report = plan.request.preflight()
        plan.recheck_tools()
        self.sandbox.require_available()
        arguments = {
            "inputs": [item.path for item in plan.request.inputs],
            "parameters": plan.recipe.bind_parameters(plan.request.parameters),
            "context": plan.request.context(),
        }
        self._emit("Checking the execution boundary with trusted preflight probes.")
        if plan.recipe.is_shell:
            report["runtime"] = self.sandbox.preflight_shell(plan.recipe.pipeline,
                                                           inventory=plan.tools, **arguments)
        else:
            probe = (
                "def run(inputs, output, params, context):\n"
                "    for path in inputs:\n"
                "        if path.is_dir():\n"
                "            next(path.iterdir(), None)\n"
                "        else:\n"
                "            with path.open('rb') as handle:\n"
                "                handle.read(1)\n"
                "    output.write_bytes(b'')\n"
            )
            with self.sandbox.execute(probe, output_suffix=".bin", **arguments) as staged:
                report["runtime"] = {"backend": staged.backend, "preflight_passed": True}
        plan.request.recheck()
        report["candidate_executed"] = False
        return report

    def check(self, plan: Plan, *, use_cache: bool = False) -> TestReport:
        """Run synthetic fixtures only. A direct call checks afresh unless requested otherwise."""
        plan.request.recheck()
        plan.recheck_tools()
        plan.recipe.validate_inputs(plan.request.inputs)
        self.sandbox.require_available()
        runtime = self._runtime_fingerprint(plan)
        count = None if self.store is None or not use_cache else self.store.qualification(plan.recipe, runtime)
        if count is not None and count == len(plan.recipe.to_dict()["tests"]):
            return TestReport(plan.recipe.digest, count, cache_hit=True)
        fixture_count = len(plan.recipe.to_dict()["tests"])
        self._emit(f"Checking {fixture_count} synthetic fixture(s).")
        if fixture_count == 0 and plan.recipe.is_shell:
            self.sandbox.preflight_shell(
                plan.recipe.pipeline, inputs=[item.path for item in plan.request.inputs],
                parameters=plan.recipe.bind_parameters(plan.request.parameters),
                context=plan.request.context(),
                inventory=plan.tools,
            )
        report = check_recipe(plan.recipe, self.sandbox, inventory=plan.tools)
        if self.store is not None:
            self.store.qualify(plan.recipe, runtime, report.passed)
        return report

    def execute(self, plan: Plan, *, stdout: BinaryIO | None = None) -> dict[str, Any]:
        """Qualify, execute, validate, then publish exactly one result; never regenerate on failure."""
        if (plan.request.output_path is None) != (stdout is not None):
            raise ValueError("Provide a binary stdout stream exactly when output_path is '-'.")
        plan.request.recheck()
        plan.recheck_tools()
        plan.request.preflight()
        report = self.check(plan, use_cache=True)
        document = plan.recipe.to_dict()
        parameters = plan.recipe.bind_parameters(plan.request.parameters)
        started = datetime.now(timezone.utc).isoformat()
        tick = time.monotonic()
        self._emit(plan.recipe.description)
        execute = self.sandbox.execute_shell if plan.recipe.is_shell else self.sandbox.execute
        payload = plan.recipe.pipeline if plan.recipe.is_shell else plan.recipe.code
        with execute(
            payload, inputs=[item.path for item in plan.request.inputs], parameters=parameters,
            context=plan.request.context(), output_suffix=SUFFIXES[document["output"]["format"]],
            **({"inventory": plan.tools} if plan.recipe.is_shell else {}),
        ) as staged:
            self._emit("Validating the staged result before publication.")
            digest, size = validate_output(staged.path, document["output"], max_bytes=self.sandbox.limits.output_bytes)
            record = {
                "schema_version": 2, "recipe_digest": plan.recipe.digest, "started_at": started,
                "elapsed_seconds": time.monotonic() - tick, "as_of": plan.request.as_of,
                "parameters": parameters, "inputs": [item.to_dict() for item in plan.request.inputs],
                "output_path": None if plan.request.output_path is None else str(plan.request.output_path),
                "output_sha256": digest, "output_bytes": size, "tests_passed": report.passed,
                "fixture_cache_hit": report.cache_hit, "backend": staged.backend,
                "runtime_fingerprint": self._runtime_fingerprint(plan), "published": False,
                "execution_kind": "shell" if plan.recipe.is_shell else "python",
                "command": getattr(staged, "command", None) or plan.command_preview(),
                "tools": getattr(staged, "tools", None) or plan.tools,
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
