"""Packaged JSON contracts and strict, bounded JSON decoding."""

from importlib.resources import files
import json
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ContractError

MAX_DOCUMENT_BYTES = 262_144


def load_schema(name: str = "recipe") -> dict[str, Any]:
    """Return an independent authoritative schema without resolving external references."""
    if name not in {"recipe", "plan", "run"}:
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
    error = next(Draft202012Validator(load_schema(name)).iter_errors(value), None)
    if error is not None:
        location = ".".join(map(str, error.absolute_path)) or "<root>"
        # Do not echo rejected code, samples, or arbitrary model text in diagnostics.
        raise ContractError(f"Invalid {name} at {location}: violates {error.validator}.")
