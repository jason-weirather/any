"""Schema-owned, content-addressed programs. Validation is not a security boundary."""

import ast
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from datetime import datetime
from pathlib import PurePosixPath
import sys
from typing import Any

from jsonschema import Draft202012Validator

from .contracts import canonical_json, parse_json, validate_document
from .errors import ContractError
from .inspection import read_columns
from .request import InputBinding


class Recipe:
    """An owned recipe document, with no paths or persistent execution authority."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        data = deepcopy(dict(document))
        validate_document(data, "recipe")
        if len(canonical_json(data)) > 262144:
            raise ContractError("Recipe exceeds 256 KiB.")
        try:
            tree = ast.parse(data["code"])
            compile(tree, "<any-recipe>", "exec")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ContractError("Recipe is not a valid Python program.") from exc
        entries = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
        if len(entries) != 1:
            raise ContractError("Recipe must define exactly one top-level run(inputs, output, params, context).")
        args = entries[0].args
        if (len(args.posonlyargs) + len(args.args) != 4 or args.vararg or args.kwarg or args.kwonlyargs
                or args.defaults):
            raise ContractError("run must accept four positional arguments, without defaults or variadic arguments.")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    raise ContractError("Relative imports are not supported in recipes.")
                roots = [node.module.split(".")[0]]
            else:
                continue
            if any(name not in sys.stdlib_module_names for name in roots):
                raise ContractError("This runtime only provides Python standard-library imports.")
        for item in data["inputs"]:
            if (item["kind"] == "directory") != (item["format"] == "directory"):
                raise ContractError("Directory input kind and format must agree.")
        for item in [*data["inputs"], data["output"]]:
            if item["required_columns"] and item["format"] not in {"csv", "tsv"}:
                raise ContractError("required_columns applies only to CSV/TSV contracts.")
        if not any(case["expected_output"] is not None for case in data["tests"]):
            raise ContractError("At least one fixture must have an expected successful output.")
        for case in data["tests"]:
            names = set()
            for file in case["files"]:
                index, relative = file["input_index"], file["path"]
                if index >= len(data["inputs"]):
                    raise ContractError("Fixture refers to a nonexistent input index.")
                if data["inputs"][index]["kind"] == "file":
                    if relative:
                        raise ContractError("A file-input fixture uses an empty relative path.")
                else:
                    validate_relative_path(relative)
                if file.get("modified_at") is not None:
                    try:
                        timestamp = datetime.fromisoformat(file["modified_at"])
                    except ValueError as exc:
                        raise ContractError("Fixture modified_at must be an aware ISO timestamp.") from exc
                    if timestamp.utcoffset() is None:
                        raise ContractError("Fixture modified_at must include a timezone offset.")
                key = index, relative
                if key in names:
                    raise ContractError("Fixture contains duplicate file paths.")
                names.add(key)
            for index, item in enumerate(data["inputs"]):
                if item["kind"] == "file" and (index, "") not in names:
                    raise ContractError("Each file input needs fixture content in every test.")
        self._document = data
        self._digest = sha256(canonical_json(data)).hexdigest()
        self.bind_parameters({})
        for case in data["tests"]:
            self.bind_parameters(case["parameters"])

    @classmethod
    def from_json(cls, text: str | bytes) -> "Recipe":
        data = parse_json(text)
        if not isinstance(data, dict):
            raise ContractError("Recipe must be a JSON object.")
        return cls(data)

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def code(self) -> str:
        return self._document["code"]

    @property
    def description(self) -> str:
        return self._document["description"]

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._document)

    def bind_parameters(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        """Apply only declared scalar parameters; no implicit string-to-number coercion."""
        definitions = self._document["parameters"]
        unknown = overrides.keys() - definitions.keys()
        if unknown:
            raise ContractError(f"Unknown recipe parameter(s): {', '.join(sorted(unknown))}")
        values = {name: deepcopy(spec["default"]) for name, spec in definitions.items()}
        values.update(overrides)
        canonical_json(values)
        for name, value in values.items():
            if not Draft202012Validator({"type": definitions[name]["type"]}).is_valid(value):
                raise ContractError(f"Parameter {name!r} must be {definitions[name]['type']}.")
        return values

    def validate_inputs(self, inputs: tuple[InputBinding, ...]) -> None:
        """Check kinds and required columns on every binding, including cached replays."""
        requirements = self._document["inputs"]
        if len(inputs) != len(requirements):
            raise ContractError(f"Recipe requires {len(requirements)} inputs; received {len(inputs)}.")
        for binding, requirement in zip(inputs, requirements, strict=True):
            binding.recheck()
            if binding.kind != requirement["kind"]:
                raise ContractError(f"Wrong input kind for {binding.path}.")
            if requirement["format"] in {"csv", "tsv"}:
                columns = read_columns(binding.path, "," if requirement["format"] == "csv" else "\t")
                missing = set(requirement["required_columns"]) - set(columns)
                if missing:
                    raise ContractError(f"Missing required columns in {binding.path}: {sorted(missing)}")

    def __repr__(self) -> str:
        return f"Recipe({self.digest[:12]}, inputs={len(self._document['inputs'])}, tests={len(self._document['tests'])})"


def validate_relative_path(value: str) -> None:
    """Fixture writers accept plain relative paths, never aliases or traversal."""
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/"))
            or "\\" in value or "\x00" in value):
        raise ContractError("Fixture path must be a nonempty, non-traversing relative POSIX path.")
