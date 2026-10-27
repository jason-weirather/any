"""Small native-command pipelines; validation is never an execution sandbox.

Arguments are literal values or explicit references, not shell fragments. The
renderer quotes every argument and emits the Bash that the confined runner uses.
Embedded awk/sed/jq/Miller expressions are still untrusted programs: the operating
system boundary, rather than this catalogue, constrains their effects.
"""

from collections.abc import Mapping, Sequence
import math
import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any

from .errors import ContractError


COMMANDS = (
    "cat", "sort", "uniq", "cut", "wc", "head", "tail", "grep", "sed", "tr",
    "comm", "join", "find", "awk", "jq", "mlr", "printf",
)
MAX_STAGES = 16
MAX_ARGUMENTS = 128
MAX_ARGUMENT_LENGTH = 8192

_NOTES = {
    "sort": "Use stdout, not -o. GNU and BSD options differ; options are not probed.",
    "uniq": "Use stdin/stdout or one input operand; do not name an output operand.",
    "head": "An expected upstream SIGPIPE is accepted only when downstream stages succeed.",
    "tail": "Finite reads only; follow mode is unsupported.",
    "grep": "Exit 1 means no matches, not an execution error; use -e for parameter patterns.",
    "sed": "No in-place editing. Expressions remain untrusted and confined.",
    "find": "No exec, deletion, or file-output actions. Use declared input references.",
    "awk": "Expressions remain untrusted and confined; no extension/include options.",
    "jq": "Available only if installed. Use structured JSON parsing, not text-field splitting.",
    "mlr": "Available only if installed. CSV-aware processing; no in-place editing.",
    "bash": "Trusted pipeline runtime only; cannot be requested as a pipeline stage.",
}


def discover_commands(names: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """Locate a bounded catalogue without executing binaries or loading shell startup.

    Relative/empty PATH entries are ignored. Availability does not imply that a
    version, flag, or data interpretation has been tested. Absolute executable
    paths retain their symlink spelling (which can affect multicall binaries).
    """
    selected = list(COMMANDS) if names is None else names
    if (not isinstance(selected, (list, tuple)) or len(selected) > 32
            or any(not isinstance(name, str) or not name or len(name) > 64 for name in selected)):
        raise ContractError("Command discovery requires at most 32 short command names.")
    search_path = os.pathsep.join(
        value for value in os.environ.get("PATH", os.defpath).split(os.pathsep)
        if value and Path(value).is_absolute()
    )
    result = {}
    for name in dict.fromkeys(selected):
        supported = name in COMMANDS or name == "bash"
        located = shutil.which(name, path=search_path) if supported else None
        result[name] = {
            "supported": supported,
            "pipeline_allowed": name in COMMANDS,
            "available": located is not None,
            "path": os.path.abspath(located) if located else None,
            "platform": sys.platform,
            "implementation": "unprobed",
            "features_verified": False,
            "notes": _NOTES.get(name, "Executable location only; version and options are unprobed.")
            if supported else "Not in the supported command catalogue; choose an available command.",
        }
    return result


def _string(value: Any) -> str:
    if isinstance(value, str):
        result = value
    elif value is None:
        result = "null"
    elif isinstance(value, bool):
        result = "true" if value else "false"
    elif isinstance(value, (int, float)) and (not isinstance(value, float) or math.isfinite(value)):
        result = str(value)
    else:
        raise ContractError("Command references must resolve to finite scalar values.")
    if "\x00" in result or len(result) > MAX_ARGUMENT_LENGTH:
        raise ContractError("A command argument contains NUL or exceeds 8192 characters.")
    return result


def _check_options(command: str, arguments: Sequence[Any]) -> None:
    """Reject a few explicit effects, not pretend to prove arbitrary utility safety.

    Called again after references are substituted, so parameter-supplied options
    cannot bypass these checks. Literal operands after -- are never options.
    """
    options = []
    for argument in arguments:
        if argument == "--" and command != "find":
            break
        if isinstance(argument, str):
            options.append(argument)
    for value in options:
        forbidden = False
        if command == "find":
            forbidden = value in {
                "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
                "-fprintf", "-fls",
            }
        elif command == "sort":
            forbidden = (bool(_short_flags(value, "koSTt") & {"o", "T"})
                         or value.startswith(("--output", "--compress-program", "--temporary-directory")))
        elif command == "sed":
            forbidden = value.startswith("--in-place") or "i" in _short_flags(value, "efli")
        elif command == "awk":
            forbidden = (bool(_short_flags(value, "FvfeliW") & {"i", "l"})
                         or value.startswith(("--include", "--load")))
        elif command == "tail":
            forbidden = value.startswith("--follow") or bool(_short_flags(value, "cn") & {"f", "F"})
        elif command == "mlr":
            forbidden = value == "-I" or value.startswith("--in-place")
        if forbidden:
            raise ContractError(f"{command}: option {value!r} is outside the supported stdout pipeline.")


def _short_flags(value: str, takes_argument: str) -> set[str]:
    """Recognize combined short flags without interpreting their attached values."""
    if not value.startswith("-") or value.startswith("--"):
        return set()
    flags = set()
    for letter in value[1:]:
        flags.add(letter)
        if letter in takes_argument:
            break
    return flags


def validate_pipeline(pipeline: Any, input_count: int, parameters: Mapping[str, Any]) -> None:
    """Validate the small composition format and its declared references."""
    if not isinstance(pipeline, list) or not 1 <= len(pipeline) <= MAX_STAGES:
        raise ContractError("A shell pipeline requires 1 to 16 stages.")
    for index, stage in enumerate(pipeline):
        if not isinstance(stage, dict) or set(stage) != {"argv"}:
            raise ContractError(f"Pipeline stage {index + 1} must contain only argv.")
        arguments = stage["argv"]
        if not isinstance(arguments, list) or not 1 <= len(arguments) <= MAX_ARGUMENTS:
            raise ContractError(f"Pipeline stage {index + 1} requires 1 to 128 arguments.")
        if not isinstance(arguments[0], str) or arguments[0] not in COMMANDS:
            raise ContractError("The first argument must be a supported literal command name.")
        for argument in arguments[1:]:
            if isinstance(argument, str):
                _string(argument)
                continue
            if not isinstance(argument, dict) or len(argument) != 1:
                raise ContractError("Arguments must be strings or one explicit input/param/context reference.")
            if "input" in argument:
                value = argument["input"]
                if type(value) is not int or not 0 <= value < input_count:
                    raise ContractError("Pipeline refers to a nonexistent input index.")
            elif "param" in argument:
                value = argument["param"]
                if not isinstance(value, str) or value not in parameters:
                    raise ContractError("Pipeline refers to an undeclared parameter.")
            elif argument != {"context": "now"}:
                raise ContractError("The only supported context reference is now.")
        _check_options(arguments[0], arguments[1:])


def resolve_pipeline(
    pipeline: list[dict[str, Any]], inputs: Sequence[Any], parameters: Mapping[str, Any],
    context: Mapping[str, Any], inventory: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[list[str]]:
    """Bind each typed reference and freeze absolute executable paths.

    The runner supplies the inputs' sandbox-visible paths and the inventory it
    discovered before building the boundary. No task command runs during binding.
    """
    validate_pipeline(pipeline, len(inputs), parameters)
    inventory = discover_commands([stage["argv"][0] for stage in pipeline]) if inventory is None else inventory
    result = []
    for stage in pipeline:
        command, *arguments = stage["argv"]
        found = inventory.get(command, {})
        executable = found.get("path")
        if (not found.get("available") or not isinstance(executable, str)
                or not Path(executable).is_absolute()):
            raise ContractError(f"Required command {command!r} is unavailable; install it explicitly or replan.")
        bound = [executable]
        for argument in arguments:
            if isinstance(argument, str):
                value = argument
            elif "input" in argument:
                value = os.fspath(inputs[argument["input"]])
            elif "param" in argument:
                value = parameters[argument["param"]]
            else:
                if "now" not in context:
                    raise ContractError("Pipeline requires the bound context clock.")
                value = context["now"]
            bound.append(_string(value))
        _check_options(command, bound[1:])
        result.append(bound)
    return result


def _early_consumer_statuses(arguments: Sequence[str]) -> tuple[int, ...]:
    command = Path(arguments[0]).name
    if command == "head":
        return (0,)
    if command != "grep":
        return ()
    early = False
    maximum = None
    index = 1
    while index < len(arguments):
        value = arguments[index]
        index += 1
        if value == "--":
            break
        if value in {"--quiet", "--silent"}:
            early = True
        elif value == "--max-count":
            early = True
            maximum = arguments[index] if index < len(arguments) else None
            index += 1
        elif value.startswith("--max-count="):
            early = True
            maximum = value.partition("=")[2]
        elif value in {
            "--regexp", "--file", "--after-context", "--before-context", "--context",
            "--binary-files", "--devices", "--directories", "--label", "--include",
            "--exclude", "--exclude-from", "--exclude-dir",
        }:
            index += 1
        elif value.startswith("-") and not value.startswith("--"):
            for offset, letter in enumerate(value[1:], start=1):
                if letter == "q":
                    early = True
                elif letter == "m":
                    early = True
                    maximum = value[offset + 1:]
                    if not maximum:
                        maximum = arguments[index] if index < len(arguments) else None
                        index += 1
                    break
                if letter in "efABCDd":
                    # An attached or following option argument is not another flag.
                    if offset == len(value) - 1:
                        index += 1
                    break
    if not early:
        return ()
    # grep -m 0 is a deliberate immediate empty result (status 1), unlike an
    # ordinary no-match scan which consumes its input. The last -m wins.
    try:
        zero_maximum = maximum is not None and int(maximum, 10) == 0
    except ValueError:
        zero_maximum = False
    return (0, 1) if zero_maximum else (0,)


def render_pipeline(argv_lists: Sequence[Sequence[str]]) -> str:
    """Render the exact Bash payload, including checks of every stage's status.

    grep's ordinary no-match status is successful empty output. Upstream SIGPIPE
    (141 in Bash) is accepted only when a supported later early consumer succeeds
    and all intervening stages exit 0/141. Other failures always fail the task.
    Early exits buried in arbitrary sed/awk expressions are intentionally not
    inferred: unexpected SIGPIPE fails rather than hiding a possibly real error.
    """
    if not argv_lists or len(argv_lists) > MAX_STAGES:
        raise ContractError("A rendered pipeline requires 1 to 16 stages.")
    for arguments in argv_lists:
        if (not arguments or len(arguments) > MAX_ARGUMENTS
                or any(not isinstance(value, str) for value in arguments)):
            raise ContractError("A rendered command requires literal string arguments.")
        for value in arguments:
            _string(value)
        if Path(arguments[0]).name not in COMMANDS:
            raise ContractError("Cannot render a command outside the supported catalogue.")
        _check_options(Path(arguments[0]).name, arguments[1:])
    lines = [
        "# Bash payload; stdout is the runner's staged output.",
        "set +e",  # Capture all PIPESTATUS entries before interpreting any failure.
        "set -o pipefail",
        " | \\\n  ".join(shlex.join(arguments) for arguments in argv_lists),
        'any_pipeline_status=("${PIPESTATUS[@]}")',
        "any_pipeline_failed=0",
    ]
    for index, arguments in enumerate(argv_lists):
        status = f"any_pipeline_status[{index}]"
        accepted = [f"{status} == 0"]
        command = Path(arguments[0]).name
        if command == "grep":
            accepted.append(f"{status} == 1")
        for consumer in range(index + 1, len(argv_lists)):
            consumer_statuses = _early_consumer_statuses(argv_lists[consumer])
            if not consumer_statuses:
                continue
            consumer_check = " || ".join(
                f"any_pipeline_status[{consumer}] == {value}" for value in consumer_statuses
            )
            conditions = [f"{status} == 141", f"({consumer_check})"]
            conditions.extend(
                f"(any_pipeline_status[{middle}] == 0 || any_pipeline_status[{middle}] == 141)"
                for middle in range(index + 1, consumer)
            )
            accepted.append("(" + " && ".join(conditions) + ")")
        lines.extend([
            "if ! (( " + " || ".join(accepted) + " )); then",
            f"  printf 'any: pipeline stage {index + 1} ({command}) exited %s\\n' "
            f'"${{any_pipeline_status[{index}]}}" >&2',
            "  any_pipeline_failed=1",
            "fi",
        ])
    lines.extend(['exit "$any_pipeline_failed"', ""])
    return "\n".join(lines)
