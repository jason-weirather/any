# any

Turn a request into ordinary commands, then keep a useful recipe for the next run.

```bash
any "Sort the lines and remove duplicates" -i words.txt -o unique.txt -v
```

`any` prefers a native command or Bash pipeline. It discovers supported installed
commands, gives that information to your existing Sheetbend model, and allows bounded
planning rounds for lookups and corrections. A small Python standard-library program
is a fallback when the available commands cannot express the task correctly.

The accepted recipe runs inside an operating-system boundary, with read-only inputs
and one staged result. Trusted code validates and publishes that result. A matching
cached request reuses the implementation, with fresh permissions and current input
checks on every invocation.

The repository is `any`; the distribution is `sheetbend-any`, its import package
is `sheetbend_any`, and the executable is **`any`**. Version 0.2 adds native pipelines
and keeps compatibility with explicit version-1 Python recipes. This remains an
experimental implementation, not an independently audited security product. See
[the execution contract](docs/security.md) and [the test and debugging guide](docs/testing.md).

## Install with current Sheetbend

Python **3.13+**, Linux or macOS. Keep your existing Sheetbend 0.5 configuration and
checkout. With the two repositories beside one another:

```bash
python -m pip install -e '../sheetbend[llm]'
python -m pip install -e '.[dev]'
any --version
```

No Sheetbend code change or new connection registry is required. Integration uses
`Registry.from_file`, source selection, `connect(requires={"json_schema"})`, and
`model.prompt(..., schema=..., stream=False)`. The planning loop runs in `any`; it
does not require provider-native function calling. Transport failures stop the
invocation rather than silently retrying or selecting another source.

Linux requires Bubblewrap (`bwrap`) with `--size` support, `libseccomp.so.2`, and Bash
for native pipelines. System tools are discovered from absolute `PATH` entries.
`any` does not install packages or run tool binaries to probe their versions.
Unsupported runtimes fail closed. On macOS, the adapter uses `/usr/bin/sandbox-exec`;
actual behavior must be checked on the target OS and Python distribution.

```bash
any --list-tools
any --doctor -vv
```

### macOS: work from ~/Scratch/Test

You can keep your data and results in `~/Scratch/Test`, and have `any` make
private scratch workspaces in `~/Scratch` instead of `/var/folders/...`:

```bash
mkdir -p "$HOME/Scratch/Test"
export ANY_TEMPORARY_DIRECTORY="$HOME/Scratch"
any --doctor -vv
```

Alternatively pass `--temporary-directory "$HOME/Scratch"` on individual
invocations. Jobs still use unique owner-only (0700) per-run subdirectories,
removed after execution. They cannot overlap a directory granted as an input:
if you explicitly pass `-i ~/Scratch`, use a different workspace parent.
This is **not** a relaxation of network, input, or output permissions.

The macOS adapter passes canonicalized paths to the worker, matching the
Seatbelt literal grants. `/var` and `/tmp` may otherwise refer to
`/private/var` and `/private/tmp`. No broad read permission to either location
is added. The doctor remains mandatory before real execution.

`--list-tools` reports metadata only: presence, selected path, platform, and known
usage notes. `features_verified: false` means flags were not probed. The doctor uses
synthetic data to test Python and native-pipeline confinement; it never contacts a
model. A failed boundary check must be resolved before execution.

## Start without a model

The included native recipe runs `sort` followed by `uniq`:

```bash
any --recipe-file examples/sort-unique.recipe.json \
  -i examples/words.txt -o - --dry-run -vv --no-cache

any --recipe-file examples/sort-unique.recipe.json \
  -i examples/words.txt -o - --check --no-cache

any --recipe-file examples/sort-unique.recipe.json \
  -i examples/words.txt -o - -v --no-cache
```

The last command writes only this result to stdout:

```text
alpha
beta
```

`-v` writes the actual Bash payload and its input bindings to stderr. The payload
contains quoted, resolved paths and pipeline-status checks. On Linux its input paths
are sandbox paths; a host-bound preview is labeled separately. Copying the payload
outside `any` does not reproduce its confinement or publication rules.

The version-1 Python example remains available:

```bash
any --recipe-file examples/filter-cd3.recipe.json \
  -i examples/cells.csv -o - -v --no-cache
```

## Generate and diagnose a recipe

Use your configured local source and model, optionally selected with `--source`,
`--model`, and `--config-path`. The model must declare `json_schema` support in
Sheetbend. Its configured reasoning default is respected; `--reasoning` is an
explicit override.

```bash
# Model/contract diagnosis without requiring a working execution boundary.
any "Sort the lines and remove duplicates" \
  -i examples/words.txt -o - --plan-only --regenerate -vv

# Reuse that candidate and check actual IO plus the execution boundary.
any "Sort the lines and remove duplicates" \
  -i examples/words.txt -o - --dry-run -vv

# Reuse, check fixtures when present, execute, validate, and publish.
any "Sort the lines and remove duplicates" \
  -i examples/words.txt -o - -v
```

Keep task text and selection options identical when testing cache reuse.
`--planning-attempts` defaults to 4, with a maximum of 8. A round can request command
metadata or submit a candidate. Invalid JSON, contracts, unavailable commands, and
unsupported options produce bounded feedback for the next round. No candidate is
executed during planning. An execution failure does not change the accepted recipe;
use `--regenerate` deliberately for a new candidate.

Only local sources are allowed by default. `--allow-institutional` and
`--allow-external` add those scopes after local in selection priority. An explicit
`--source` remains subject to that allow-list. No failed request crosses a scope
boundary automatically.

Generation sends the task, input metadata, and bounded previews: at most
`--sample-bytes` per file, 32 immediate names per directory, and 16 KiB total preview
content. `--sample-bytes 0` sends metadata without file contents or child names.
The actual operation always uses the full bound inputs.

## Inspection and verbosity

| Option | Behavior |
| --- | --- |
| Default | Quiet success; requested result only on stdout. |
| `-v` | Actual accepted computation on stderr, including resolved Bash bindings or fallback Python. |
| `-vv` | Also show dependency versions, source/model/scope, reasoning, planning rounds, validation feedback, bound plan, and sandbox launch diagnostics. |
| `-vvv` | Also show bounded rejected model responses, selected provider error fields, and failure tracebacks. Logs may contain input data. |
| `--plan-only` | Prepare/display a candidate and check input readability; no sandbox probe, output-parent write probe, fixtures, task execution, or publication. |
| `--dry-run`, `--dryrun` | Prepare/display; open declared input roots; probe output-parent creation/publication operations with private files; run trusted boundary/readability probes. Never execute the candidate or its fixtures. |
| `--check` | Run supplied synthetic fixtures through the real boundary. A shell recipe with no fixtures reports zero and performs trusted boundary preflight. Never perform the task on real input data. |
| `--history` | Show the last 100 successful cached run receipts, including the executed payload. No inference. |

Plans and diagnostic output go to stderr. Explicit inspection commands (`--doctor`,
`--list-tools`, `--history`, `--list-recipes`, `--check`) return their JSON report on
stdout. Capture a failing diagnostic run with `2> any-debug.log`. Headers and complete
provider requests are not logged; recognizable credentials are redacted, but model
responses and paths can still contain sensitive content. Review logs before sharing.

Dry-run probes never open the real output for writing. They do create and remove
private files in its parent. They cannot guarantee later success under changed
permissions, target-specific restrictions, disk exhaustion, or concurrent changes.
Directory-root readability does not prove that every descendant is readable.

## Supported command shape

A shell recipe is a list of argument-vector stages connected by pipes. Each argument
is a literal string or a whole input/parameter/time reference. The coordinator binds
those references and quotes them before constructing the actual Bash payload. There
is no model-authored `eval`, shell interpolation, redirect, loop, or background syntax.

The initial catalog covers `cat`, `sort`, `uniq`, `cut`, `wc`, `head`, `tail`, `grep`,
`sed`, `tr`, `comm`, `join`, `find`, `awk`, `jq`, `mlr`, and `printf`. Only installed
commands are available. Bash is the trusted pipeline runtime, not a selectable stage.
The catalog rejects known incompatible effects such as in-place writes, `sort -o`,
`find -exec`, and indefinite `tail -f`; it is not a substitute for OS confinement.

Pipeline failures are checked per stage. A normal `grep` no-match is an empty result;
expected upstream SIGPIPE for supported early consumers is handled explicitly.
Native processing uses `LC_ALL=C`. GNU/BSD differences and unprobed flags remain
possible failures. The planner should use format-aware tools: `awk -F,` does not
correctly parse general quoted CSV. When a clear native solution is unavailable,
a justified Python fallback is preferable to incorrect parsing.

A Python fallback must include a reason and at least one successful synthetic fixture.
Shell fixtures are optional. A passing model-authored fixture is useful evidence,
not proof of semantic correctness.

## Output authority

| Invocation | Authority |
| --- | --- |
| `-o -` | Buffer and validate the result, then send its bytes to stdout. |
| `-o result.csv` | Create one new regular file; refuse replacement. |
| `-o result.csv --overwrite` | Replace that exact output file. |
| `-i file.csv -o file.csv --modify file.csv` | Explicitly replace that input through staging. |

`--overwrite` never grants input modification. Task text grants no filesystem
permissions. Final-component symlinks, devices, FIFOs, and multiply linked output
files are rejected. Output parents must already exist; outputs inside input directory
grants are rejected. There is one output file, with no general deletion, bulk rename,
append, or multi-file transaction mode.

Even with `--modify`, originals stay read-only in the worker. Trusted publication
handles replacement. With `any ... -o - > result.csv`, the calling shell owns that
redirection; use `-o result.csv` for `any`'s no-overwrite protection.

## Save and replay

```bash
any --recipe-file examples/sort-unique.recipe.json \
  -i examples/words.txt -o - --save-recipe unique-lines

any --recipe unique-lines -i another.txt -o another-unique.txt -v
any --list-recipes
any --history
```

Names identify immutable recipes, never permanent file permissions. A different
implementation needs a new name. `--recipe` accepts a literal name or full SHA-256
digest; `--recipe-file` explicitly imports a recipe subject to the same confinement.

Automatic lookup uses the exact task, ordered canonical input paths, output suffix,
inspection settings, and explicit compiler choices. Changed data at the same path
reuses the implementation after fresh contract checks. Cache hits make no model
request. Fixture qualification is bound to the recipe, selected tool identities,
and observed runtime fingerprint. Every execution still establishes the boundary
and checks its canaries. A changed selected executable invalidates a prepared plan.

Relative dates use recorded `context['now']`; `--as-of` fixes it for replay. This is
an instruction and binding facility, not a proof of arbitrary program time-purity.

## Library stages

```python
from pathlib import Path
from sheetbend_any import Engine, Plan, Recipe, Request, Sandbox

request = Request.bind(None, input_paths=["examples/words.txt"], output_path="unique.txt")
recipe = Recipe.from_json(Path("examples/sort-unique.recipe.json").read_bytes())
plan = Plan.bind(request, recipe)
engine = Engine(store=None, sandbox=Sandbox())

print(plan.preview())       # Does not execute the candidate or fixtures.
engine.preflight(plan)      # Trusted IO and boundary probes only.
report = engine.check(plan) # Synthetic fixtures only.
receipt = engine.execute(plan)
```

For generation use `Request.bind("task", ...)` and `engine.prepare(request)`.
`Sandbox.execute_shell()` and `Sandbox.execute()` expose staged results through
context managers; consume them before their contexts close. `testing.check_recipe`,
`inspection.inspect_inputs`, `output.validate_output`, and `output.publish_output`
remain independent stages. The CLI uses these same implementations.

## State and limits

State remains in `${XDG_CACHE_HOME:-$HOME/.cache}/any`, configurable with
`--cache-directory`. Its directory/database are private (0700/0600). Up to 256 unnamed
recipes and 100 run receipts are retained; named recipes are not automatically
removed. History is cache state, not a permanent audit log under `.config`.
`--no-cache` disables all `any` cache reads/writes and receipts. Sheetbend owns its
separate runtime ledger. Generated code/descriptions can contain input data; this
plaintext cache is not a secret vault.

Defaults per worker: 30 seconds, 512 MiB memory, 64 MiB output/per-file size, 128 MiB
scratch, and a supervised 64-process cap. Native tools with large virtual-memory
reservations can require an explicit higher `--memory-mib`. No limit is relaxed
automatically. See [security details](docs/security.md) for hard versus supervised
limits. These settings do not control Sheetbend's queue or provider timeout.

JSON output validation is limited to 8 MiB; text/table validation and hashing stream.
CSV parsing retains Python's default field-size bound. Fixture expectations compare
exact UTF-8 output or an ordinary program failure. Binary output remains byte-preserving,
but binary fixtures are not yet convenient. No third-party Python packages, package
installation, network-enabled worker, or unrestricted host-command mode are provided.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
python -m pip wheel --no-deps --wheel-dir dist .
ANY_REQUIRE_SANDBOX=1 ANY_REQUIRE_INFERENCE=1 pytest
```

The strict gates turn unavailable integration into failures. Tests distinguish
orchestration doubles, actual native confinement, and the real Sheetbend/LLM/SDK
protocol against a loopback test server. They do not test the quality or availability
of your configured live model. See [recommended commands](docs/testing.md).
