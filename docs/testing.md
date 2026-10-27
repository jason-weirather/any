# Test and debug the command-pipeline update

Run these from the `any` checkout with its Python 3.13+ environment active. Keep the
existing Sheetbend 0.5 checkout/configuration. The patch does not require a new
Sheetbend API, native function calling, or a second endpoint configuration.

## 1. Install and inspect without inference

```bash
python -m pip install -e '../sheetbend[llm]'
python -m pip install -e '.[dev]'
python -m pip check
any --version
any --list-tools
```

The version should be `0.2.0`. Tool metadata should show installed paths for `bash`,
`sort`, and `uniq`. Discovery never executes tool binaries, so `features_verified`
is false. Non-system Linux native dependencies outside the exposed runtime roots
can still fail at execution; no broad installation/home-directory grant is added.

Create a private directory for logs and test outputs:

```bash
umask 077
ANY_CHECK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/any-check.XXXXXX")
printf 'Test files: %s\n' "$ANY_CHECK_DIR"
```

Leave it in place while investigating. Logs can contain filenames, model-generated
content, and bounded data previews even after recognizable credentials are redacted.

### macOS: keep tests in ~/Scratch/Test

Put ordinary test inputs and results in `~/Scratch/Test`, and private job
workspaces in the parent `~/Scratch`. This avoids system temp path aliases
without disabling any sandbox checks:

```bash
mkdir -p "$HOME/Scratch/Test"
export ANY_TEMPORARY_DIRECTORY="$HOME/Scratch"
printf 'beta\nalpha\nbeta\n' > "$HOME/Scratch/Test/words.txt"
any --doctor -vv

any --recipe-file examples/sort-unique.recipe.json \
  -i "$HOME/Scratch/Test/words.txt" \
  -o "$HOME/Scratch/Test/unique.txt" --dryrun -vv
any --recipe-file examples/sort-unique.recipe.json \
  -i "$HOME/Scratch/Test/words.txt" \
  -o "$HOME/Scratch/Test/unique.txt" -v
cat "$HOME/Scratch/Test/unique.txt"
```

The last `cat` should output `alpha` and `beta`, one per line. To rerun after
successful publication, use `--overwrite` only for that explicit output.
`ANY_TEMPORARY_DIRECTORY` is used only by the trusted coordinator, not
forwarded to the job. You can instead pass `--temporary-directory
"$HOME/Scratch"` for an individual `any` invocation.

The temporary workspaces are still separate, unique, mode-0700 directories
that are removed on ordinary completion. They protect concurrent jobs and
staging. **Do not use the workspace parent itself as a declared input
directory** (`-i ~/Scratch`); the security check deliberately rejects that
overlap. Using `-i ~/Scratch/Test` is fine if output is elsewhere and the
workspace is in the sibling directory under `~/Scratch`.

A macOS regression previously showed worker input `/var/folders/...` while its
Seatbelt grant named `/private/var/folders/...`. The adapter now canonicalizes
worker inputs to match their exact read grants and permits only **metadata**
for required symlink traversal. It does not authorize the `/private/var`
subtree. The actual Mac `--doctor` canary still must pass before execution.

## 2. Check the actual OS boundary without a model

```bash
any --doctor -vv \
  > "$ANY_CHECK_DIR/doctor.json" \
  2> "$ANY_CHECK_DIR/doctor.log"
```

Success reports `python_canaries_passed` and `shell_canaries_passed` as true. The
probe includes inherited child confinement and a native `cat | wc` round-trip.
A failure here is independent of model quality. Inspect the backend, launched
command, reported signal/status, and underlying stderr. Do not disable canaries
or add an unrestricted execution fallback to work around a failure.

On macOS this check must run on the actual deployment; Linux integration results do
not validate Seatbelt. Native macOS descendant cleanup remains best effort, as
explained in [the execution contract](security.md). Published bytes come from a
separate coordinator-owned snapshot that workers cannot modify.

## 3. Test an authored native recipe, without Sheetbend

```bash
any --recipe-file examples/sort-unique.recipe.json \
  -i examples/words.txt -o "$ANY_CHECK_DIR/authored.txt" \
  --dryrun --no-cache -vv 2> "$ANY_CHECK_DIR/authored-dryrun.log"

test ! -e "$ANY_CHECK_DIR/authored.txt"

any --recipe-file examples/sort-unique.recipe.json \
  -i examples/words.txt -o - --check --no-cache

any --recipe-file examples/sort-unique.recipe.json \
  -i examples/words.txt -o "$ANY_CHECK_DIR/authored.txt" \
  --no-cache -v 2> "$ANY_CHECK_DIR/authored-run.log"

printf 'alpha\nbeta\n' > "$ANY_CHECK_DIR/expected.txt"
diff -u "$ANY_CHECK_DIR/expected.txt" "$ANY_CHECK_DIR/authored.txt"
```

Expected: dry-run creates no task output, `--check` reports one passing fixture,
and `diff` is silent. The run log contains the actual confined `sort | uniq` payload
plus its status handling. Normal success with `-o FILE` writes no task stdout.

Confirm no-overwrite behavior by repeating the last `any` invocation without
`--overwrite`: it must fail, leaving the existing output unchanged. Adding
`--overwrite` authorizes that exact file. This does not authorize overwriting an
input; input replacement still requires `--modify`.

Check the original Python path as well:

```bash
any --recipe-file examples/filter-cd3.recipe.json \
  -i examples/cells.csv -o - --no-cache -v \
  2> "$ANY_CHECK_DIR/python-run.log"
```

Expected CSV rows: IDs `003` and `004`, in the original order.

## 4. Isolate Sheetbend planning

Choose the existing source/model if defaults are not sufficient. Use the **same**
selection options for all subsequent invocations:

```bash
ANY_MODEL_ARGS=()
# Optional examples; replace these names with entries in your existing configuration:
# ANY_MODEL_ARGS=(--source my-local-source --model my-model)
# ANY_MODEL_ARGS+=(--config-path /path/to/sheetbend.toml)

ANY_TASK='Sort the input lines in ascending order and remove duplicates. Write plain text.'

any "$ANY_TASK" "${ANY_MODEL_ARGS[@]}" \
  -i examples/words.txt -o "$ANY_CHECK_DIR/generated.txt" \
  --cache-directory "$ANY_CHECK_DIR/cache" \
  --plan-only --regenerate -vvv \
  2> "$ANY_CHECK_DIR/planning.log"

test ! -e "$ANY_CHECK_DIR/generated.txt"
```

`--plan-only` makes model requests, validates/binds the candidate, and opens declared
input roots. It does not require sandbox availability and does not probe output-parent
writes. It has no path that executes a task or publishes a result.

The log should identify installed `any`, Sheetbend, `llm`, and SDK versions; chosen
source/model/scope; reasoning; request/schema/response sizes; round durations; and
accepted recipe identity. Invalid candidates get a specific rejection and another
round within the budget. `-vvv` additionally exposes bounded rejected response text
and selected provider error details. A transport/configuration failure is not
retried through another source.

If a small local model repeatedly returns a 100-200 byte shell candidate and
reports `Invalid recipe ... oneOf`, the planner now accepts a compact shell recipe
and builds the complete recipe in trusted code. Use `--reasoning off` to avoid
long reasoning passes when testing tiny models, and `--planning-attempts 1 -vvv`
to inspect one rejected response (which may contain input data). The local runtime
still validates the complete recipe before saving/executing anything.

For this task the expected solution is `sort`/`uniq` or equivalent native `sort -u`.
If it selects Python, inspect `fallback_reason` and tool inventory. Planning rounds
default to four; `--planning-attempts 6` can help diagnose a model that spends rounds
on metadata requests, but it is not a remedy for an endpoint or schema-support error.

## 5. Reuse the candidate for dry-run, execution, and history

```bash
any "$ANY_TASK" "${ANY_MODEL_ARGS[@]}" \
  -i examples/words.txt -o "$ANY_CHECK_DIR/generated.txt" \
  --cache-directory "$ANY_CHECK_DIR/cache" --dry-run -vv \
  2> "$ANY_CHECK_DIR/generated-dryrun.log"

test ! -e "$ANY_CHECK_DIR/generated.txt"

any "$ANY_TASK" "${ANY_MODEL_ARGS[@]}" \
  -i examples/words.txt -o "$ANY_CHECK_DIR/generated.txt" \
  --cache-directory "$ANY_CHECK_DIR/cache" -v \
  2> "$ANY_CHECK_DIR/generated-run.log"

diff -u "$ANY_CHECK_DIR/expected.txt" "$ANY_CHECK_DIR/generated.txt"

any --history --cache-directory "$ANY_CHECK_DIR/cache" \
  > "$ANY_CHECK_DIR/history.json"
```

The dry-run should say it reused a cached recipe and passed preflight. It opens the
real input roots, creates/removes private output-parent probes, and runs only trusted
boundary/readability probes. The candidate and its fixtures are not executed.
Execution reuses the same recipe, runs any unqualified fixtures, and validates the
coordinator-owned snapshot before publication. History records the actual payload,
selected tools, backend, output checksum, and fixture count.

If you change the task, source/model options, sampling settings, planning budget,
input paths, or output suffix, you may select a different cache entry. `--no-cache`
disables both reuse and history. `--regenerate` deliberately replaces an automatic
candidate; it is not needed for an ordinary rerun.

## 6. Test real parsing, not only line manipulation

After the simple flow works, try a CSV with quoted delimiters and embedded newlines:

```bash
cat > "$ANY_CHECK_DIR/quoted.csv" <<'CSV'
id,CD3,note
a,5,"comma, inside"
b,22,"quoted ""word"""
c,41,"two
lines"
CSV

any 'Keep rows where numeric CD3 is greater than 20. Preserve columns, cell values, and row order. Handle quoted CSV and embedded newlines correctly. Write CSV.' \
  "${ANY_MODEL_ARGS[@]}" -i "$ANY_CHECK_DIR/quoted.csv" \
  -o "$ANY_CHECK_DIR/filtered.csv" --regenerate -vv \
  2> "$ANY_CHECK_DIR/csv.log"

python - "$ANY_CHECK_DIR/filtered.csv" <<'PY'
import csv
import sys
with open(sys.argv[1], newline='') as handle:
    rows = list(csv.DictReader(handle))
assert rows == [
    {'id': 'b', 'CD3': '22', 'note': 'quoted "word"'},
    {'id': 'c', 'CD3': '41', 'note': 'two\nlines'},
], rows
print('CSV values and order verified')
PY
```

A suitable installed format-aware native command is preferred. A documented Python
standard-library fallback is reasonable if the installed supported commands cannot
handle the data correctly. A naive comma-splitting pipeline should fail this semantic
check. Model-authored fixtures alone do not establish the right interpretation.

## Failure map

| Evidence | Likely layer / next check |
| --- | --- |
| Selection error before round 1 | Existing Sheetbend config, source scope, model capability, or reasoning selection. |
| HTTP/authentication/timeout/schema rejection | Provider/transport. Inspect `-vvv` fields and the real endpoint's schema support. No automatic source fallback occurs. |
| Repeated candidate rejection | Inspect exact field/command/option feedback and bounded model responses; use explicit regeneration after changing the request. |
| Planning succeeds, doctor/dry-run fails | OS sandbox, runtime dependencies, path access, or declared resource limits. |
| Doctor works, generated native command fails | Tool flags, GNU/BSD behavior, data assumptions, native dependencies, or resource use. Read stage status and stderr. |
| Output validation fails | Result does not satisfy its declared format/columns/size; task output was not published. |
| Output was published, receipt storage failed | The error explicitly says publication happened; do not assume rollback. |

Dry-run success is a preflight result, not proof the task will succeed. It cannot
prove recursive directory access, target-specific ACL/immutable restrictions, future
free disk space, or semantic correctness.

## Development gates

```bash
pytest
ruff check .
python -m pip wheel --no-deps --wheel-dir dist .
ANY_REQUIRE_SANDBOX=1 ANY_REQUIRE_INFERENCE=1 pytest
```

The strict command requires actual supported OS confinement and the real
Sheetbend/LLM/SDK stack against a loopback test server. These protocol tests exercise
selection, schema transmission, iterative feedback, response consumption, and errors;
they do not contact or certify the user's live model. CI should retain strict gates
on each supported OS/interpreter combination.
