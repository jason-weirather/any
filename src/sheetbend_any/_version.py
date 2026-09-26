"""Resolve the sole authored version from the checkout or distribution metadata."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _resolve_version() -> str:
    project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if project_file.is_file():
        with project_file.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
        if project.get("name") == "sheetbend-any":
            return project["version"]
    try:
        return version("sheetbend-any")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _resolve_version()
