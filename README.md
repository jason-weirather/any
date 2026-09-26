# any

Manufacture the missing little command, then keep the program when it proves useful.

```bash
any "Keep rows where CD3 is greater than 20, preserving columns and row order" \
  -i cells.csv -o positive.csv
```

`any` turns a request into an inspectable Python recipe, checks synthetic fixtures,
runs the program inside an operating-system boundary, validates its staged result,
and publishes only the explicitly authorized output. A matching cached request
reuses the program, not yesterday's results or yesterday's permissions.

**Experimental first release.** The permission checks and execution adapters are
implemented, but this is not an independently audited security product. Start with
synthetic/non-sensitive data and run `any --doctor` on the actual deployment.
Do not treat a passing fixture or boundary canary as proof of semantic correctness
or immunity to operating-system vulnerabilities. See [the security contract](docs/security.md).

The repository is `any`; the distribution is `sheetbend-any`, its import package
is `sheetbend_any`, and the executable is **`any`**. The distribution name is not a
claim that anything has been published or reserved on a package index.

## Install

Python **3.13+**, Linux or macOS. Use your current Sheetbend 0.5 checkout/release.
With the two repositories beside one another:

```bash
mamba create -n any_env -c conda-forge python=3.13 pip
mamba activate any_env
python -m pip install -e '../sheetbend[llm]'
python -m pip install -e '.[dev]'
any --version
```

The Linux execution adapter additionally requires **Bubblewrap (`bwrap`) with
`--size` support** and the native **`libseccomp.so.2`** library. Install those through
your system package manager. `any` never installs system packages itself. A missing
or unsupported backend fails closed; it does not fall back to `subprocess` on the
unrestricted host.

On macOS the adapter uses `/usr/bin/sandbox-exec` with a default-deny profile.
There is no Docker service or second language runtime to configure. This native
macOS adapter requires validation on the target Python distribution and OS version;
its availability is not a security certification.

```bash
any --doctor
```

The doctor uses only synthetic canaries. It checks a result round-trip, denied
reads/writes, networking, hard-link creation, and subprocess creation. It does not
contact Sheetbend. A failure must be fixed, not bypassed with an unsafe mode.

## First smoke test, without a model

The repository includes a human-authored CSV recipe and tiny data file:

```bash
any --recipe-file examples/filter-cd3.recipe.json \
  -i examples/cells.csv -o - --dry-run --no-cache

any --recipe-file examples/filter-cd3.recipe.json \
  -i examples/cells.csv -o - --check --no-cache

any --recipe-file examples/filter-cd3.recipe.json \
  -i examples/cells.csv -o - --no-cache
```

The executed result is:

```csv
cell_id,CD3,note
003,22,positive
004,41,positive
```

`--check` runs the synthetic fixtures, not the operation on your real inputs.
`--dry-run` executes neither the program nor its fixtures. Both modes still bind
and validate the supplied input/output contract.

## Generate a utility

Connection details remain in Sheetbend's existing configuration. There is no
additional endpoint registry, credential store, source-health probe, or model
fallback. The selected model must declare `json_schema` support. Its configured
reasoning default is respected; `--reasoning` explicitly overrides that choice.

**Only local processing is allowed by default.** `--allow-institutional` and
`--allow-external` add their respective scopes, after local in selection priority.
An explicit `--source` remains subject to that allow-list. A failed local request
is not retried through another source.

```bash
any "Keep rows where CD3 is greater than 20. Fail on invalid numeric values. \
Preserve all columns and row order. Write CSV" \
  -i examples/cells.csv -o positive.csv --dry-run

# Reuses the prepared candidate, then tests and runs it.
any "Keep rows where CD3 is greater than 20. Fail on invalid numeric values. \
Preserve all columns and row order. Write CSV" \
  -i examples/cells.csv -o positive.csv -v
```

One generation request produces a schema-validated candidate. There is no automatic
repair loop. A failed cached program does not silently change its implementation.
Use `--regenerate` deliberately when a new candidate is appropriate.

Only bounded input previews are sent during generation: at most `--sample-bytes`
per file, at most 32 immediate names per directory, and a combined preview budget
of 16 KiB. `--sample-bytes 0` sends root names/kinds/sizes, but no file contents or
child directory names. The task text itself is also sent. Allowing a nonlocal scope
therefore explicitly permits these request details to reach that configured source.

The full operation processes the actual bound inputs in ordinary Python. A model's
preview is never substituted for a filesystem listing or full-table computation.

## Output authority

| Invocation | Authority |
| --- | --- |
| `-o -` | Buffer and validate the result, then send its bytes to stdout. |
| `-o result.csv` | Create one new regular file; refuse replacement. |
| `-o result.csv --overwrite` | Replace that exact output file. |
| `-i file.csv -o file.csv --modify file.csv` | Explicitly replace that input through staging. |

Input modification is **not** enabled by `--overwrite`, including when an output
aliases an input. Task text such as "overwrite whatever you need" grants nothing.
Final-component symlinks, devices, FIFOs, and multiply linked output files are
rejected. Output parents must already exist.

Even with modification permission, the worker sees read-only originals. It writes
one private candidate; trusted publication code handles replacement. The original
file is not opened for writing by generated code.

The first release supports one output file, not a writable output directory.
Destinations inside an input directory are deliberately rejected. Deletion,
in-place append, bulk rename/move, and multi-file change plans are not implemented.

**Shell redirection is outside this policy.** In `any ... -o - > result.csv`, the
calling shell manages `result.csv` before `any` runs. Use `-o result.csv` for `any`'s
no-overwrite protection. Bytes already written to another process cannot be recalled.

## Save and replay

```bash
any --recipe-file examples/filter-cd3.recipe.json \
  -i examples/cells.csv -o - --save-recipe filter-cd3

any --recipe filter-cd3 \
  -i another-table.csv -o another-positive.csv --param threshold 30

any --list-recipes
```

A saved name identifies immutable code, contracts, defaults, and tests. It does not
identify permanent file permissions. Changing a saved name's program is refused;
choose a new name. `--recipe` accepts a literal name or full SHA-256 digest, never
fuzzy matching or an ambiguous digest prefix. `--recipe-file` is a separate,
explicit origin for an externally supplied recipe document; it is still untrusted
code and uses the same boundary.

For automatic cache lookup, the exact task, ordered canonical input paths, output
suffix hint, inspection setting, and explicit compiler-selection settings form the
key. Output directories, replacement permission, and run-time parameter values are
not authority carried by that key. Changing data at the same input path reuses the
program; input kinds and required columns are validated again. Named recipes are
how to deliberately reuse a utility on different paths.

A cache hit makes **no model request**. Fixture qualification is reused only for the
same recipe and observed runtime fingerprint. `--check` explicitly retests. Every
real worker still establishes its OS boundary and runs the boundary canaries.

Relative dates use the invocation's recorded `context['now']`; `--as-of` fixes that
value for a replay. Programs are instructed to use that value rather than the live
clock. This instruction is not a proof that arbitrary generated code is time-pure.
Prefer explicit cutoff timestamps for important analyses.

## Terminal behavior

Default success is quiet, apart from the requested stdout result. `-v` reports useful
execution stages on stderr. `-vv` adds the bound contract and actual program. There
is no routine "sending to Sheetbend" narration, explanation mixed into data, or
automatic printing of raw shell commands after a permission failure.

A preview includes both the actual Python program and its binding contract. It is
not a bare shell command: running the program yourself outside `any` would not
preserve the execution or publication boundary.

## Library stages

```python
from pathlib import Path
from sheetbend_any import Engine, Recipe, RecipeStore, Request, Sandbox, Plan

request = Request.bind(
    None,
    input_paths=["examples/cells.csv"],
    output_path="positive.csv",
)
recipe = Recipe.from_json(Path("examples/filter-cd3.recipe.json").read_bytes())
plan = Plan.bind(request, recipe)
engine = Engine(store=RecipeStore(), sandbox=Sandbox())

print(plan.preview())         # Does not execute code or tests.
report = engine.check(plan)   # Synthetic fixtures only.
receipt = engine.execute(plan)
```

For generation, construct `Request.bind("your task", ...)` and use
`engine.prepare(request)`. `SheetbendCompiler` exposes explicit source settings.
`Engine(store=None)` disables persistence. For stdout, bind `output_path="-"` and
supply a binary stream to `engine.execute(plan, stdout=stream)`.

`Sandbox.execute()` is a context manager exposing a staged result, and
`testing.check_recipe`, `inspection.inspect_inputs`, `output.validate_output`, and
`output.publish_output` are meaningful individual workflow stages. A staged path
must be consumed inside its context. The CLI calls these same implementations.

## State and limits

Recipe storage defaults to `${XDG_CACHE_HOME:-$HOME/.cache}/any`; override with
`--cache-directory`. Its directory is private (0700) and its SQLite database is
private (0600). At most 256 unnamed recipes and 100 run receipts are retained;
named recipes are not automatically evicted. Receipts record paths, metadata
identities, parameters, timestamps, runtime identity, and the output checksum.
Inputs are **not** content-hashed or snapshotted by default.

Raw previews and prompts are not separately stored, but generated code/descriptions
can themselves contain sensitive text. The cache is not encrypted and is not a
secret vault. `--no-cache` disables `any` cache reads/writes and run receipts;
Sheetbend still owns its separate, content-free runtime ledger.

Private workspaces are automatically cleaned and can be placed under an existing
`--temporary-directory`. Publication briefly creates a private file in the authorized
output's parent. Abrupt process/host termination can leave temporary files; ordinary
failures and cancellation have cleanup paths. Do not place private workspaces beneath
an input directory that grants the worker access to them.

Defaults per worker: 30 seconds, 512 MiB memory, 64 MiB per-file/output size, and
128 MiB scratch. See `--help` for overrides and [security details](docs/security.md)
for hard versus supervised limits. These do not control Sheetbend's request queue or
provider timeout.

## First-release scope

The useful center is file inventories, hashes, CSV/TSV transformations and comparisons,
JSON/text reports, and log extraction using Python's standard library. No Bash,
third-party Python environment, package installation, network-enabled worker, or
raw host-command escape hatch is provided.

Fixture expectations currently compare exact UTF-8 output (or an expected error).
Binary output publication is byte-preserving, but these fixtures do not yet cover
arbitrary ZIP/image artifacts conveniently. JSON result validation is limited to
8 MiB; CSV/text validation and hashing stream. CSV parsing retains Python's default
field-size bound. These limits fail explicitly rather than skipping validation.

A passing self-authored test suite is evidence, **not an independent correctness
oracle**. Review scientific transformations and any program that will replace data.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
python -m pip wheel --no-deps --wheel-dir dist .

# Strict deployment/release gates: unavailable integration is a failure.
ANY_REQUIRE_SANDBOX=1 ANY_REQUIRE_INFERENCE=1 pytest
```

Tests distinguish orchestration doubles, actual native syscall-filter checks, actual
OS confinement tests, and the real Sheetbend/LLM/SDK protocol against a loopback test
server. None is a benchmark of a local model's ability to write correct programs.
CI is configured for Linux/macOS and Python 3.13/3.14, with strict integration gates.
The API is experimental; the existing Apache-2.0 license is unchanged.
