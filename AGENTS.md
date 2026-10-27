# Working on any

This is a Python 3.13+ library with a thin Click CLI. The executable is `any`, the
repository is `any`, the distribution is `sheetbend-any`, and the import package is
`sheetbend_any`. Keep this relationship explicit rather than adding aliases.

Use pyproject.toml as the sole authored package version. External recipe/plan/run
contracts belong in the packaged JSON Schemas. Keep useful library workflow stages
public and independently usable; do not bury them in CLI callbacks.

Project-specific exception to the general output style: **never overwrite by
default**. `--overwrite` authorizes only the declared output. Input replacement needs
`--modify`. Generated code gets neither write authority over originals nor an
unrestricted execution fallback. Do not convert a failed boundary check into a
warning, remove an integration gate to make tests pass, or call an AST check a sandbox.

A cache hit reuses code, not results or permissions. Replacing a cached candidate
needs explicit regeneration. Within one new generation, bounded structured rounds
may discover command metadata and repair invalid candidates; they never execute
candidate code or retry transport errors. Prefer ordinary command pipelines, with
a justified Python standard-library fallback. Source preferences are local-first
and boundary changes are explicit. Sheetbend owns connections/configuration, not
execution or output publication. Keep using its existing structured-response API.

`-v` shows the actual bound payload on stderr; `-vv` adds workflow diagnostics;
`-vvv` can expose bounded model/provider text. Keep task stdout clean. `--dry-run`
checks real IO and sandbox preflight without running the candidate. `--plan-only`
never executes or publishes and must remain usable to diagnose inference separately.

Tests distinguish orchestration doubles from actual OS confinement and actual
Sheetbend/LLM/SDK integration. Report skipped/unavailable integration honestly.

Run pytest, ruff check ., and a wheel build. Strict release/deployment tests use:

    ANY_REQUIRE_SANDBOX=1 ANY_REQUIRE_INFERENCE=1 pytest

Do not advertise production security or semantic correctness based solely on
model-authored fixtures or synthetic boundary probes. Review docs/security.md when
changing inputs, outputs, path handling, process lifecycle, or native policies.
