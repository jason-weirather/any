"""Packaged JSON contracts and strict, bounded JSON decoding."""

from importlib.resources import files
import json
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ContractError

MAX_DOCUMENT_BYTES = 262_144


def load_schema(name: str = "recipe") -> dict[str, Any]:
    """Return an independent authoritative schema without resolving external references."""
    if name not in {"recipe", "plan", "run", "planner"}:
        raise ValueError("Unknown schema name.")
    return json.loads(files("sheetbend_any").joinpath("schemas", f"{name}.schema.json").read_text())


def canonical_json(value: Any) -> bytes:
    """Stable UTF-8 encoding. Nonfinite numbers are not part of our JSON contracts."""
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError("Expected finite, UTF-8 JSON-compatible data.") from exc


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"Duplicate JSON key: {key!r}.")
        result[key] = value
    return result


def parse_json(text: str | bytes) -> Any:
    """Reject oversized documents, duplicate keys, and nonstandard numeric constants."""
    if len(text.encode("utf-8") if isinstance(text, str) else text) > MAX_DOCUMENT_BYTES:
        raise ContractError("JSON document exceeds the 256 KiB contract limit.")
    try:
        return json.loads(
            text, object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("Expected one valid JSON document, without fences or prose.") from exc


def validate_document(value: Any, name: str) -> None:
    canonical_json(value)
    schema = load_schema(name)
    # Select the explicitly versioned branch for actionable field locations,
    # instead of reporting only a root-level oneOf failure for every typo.
    version = value.get("schema_version") if isinstance(value, dict) else None
    if name == "recipe" and type(version) in (int, float) and version in (1, 2):
        # JSON Schema treats 1 and 1.0 as the same number, but never true as 1.
        schema = schema["$defs"][f"v{int(version)}"]
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        # The full recipe has mutually exclusive shell/Python branches. A model
        # violating just one shell field should see that field, not "oneOf".
        if (name == "recipe" and error.validator == "oneOf" and isinstance(value, dict)
                and value.get("kind") in {"shell", "python"}):
            chosen = next((branch for branch in schema.get("oneOf", [])
                           if branch.get("properties", {}).get("kind", {}).get("const")
                           == value["kind"]), None)
            if chosen is not None:
                error = next(Draft202012Validator(chosen).iter_errors(value), error)
        location = ".".join(map(str, error.absolute_path)) or "<root>"
        # Do not echo rejected code, samples, or arbitrary model text in diagnostics.
        expectation = ""
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = [key for key in error.validator_value if key not in error.instance]
            expectation = f" Missing fields: {', '.join(missing)}."
        elif error.validator in {"type", "minItems", "maxItems", "minLength", "maxLength", "const"}:
            expectation = f" Expected {error.validator_value!r}."
        raise ContractError(f"Invalid {name} at {location}: violates {error.validator}.{expectation}")
