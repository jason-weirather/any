# Execution and publication contract

## Threat model

Generated commands/code, input contents/names, imported recipe files, and cached code
are untrusted. The caller, `any` coordinator, installed tools/runtime libraries,
Sheetbend configuration, native sandbox implementation, and OS/kernel are trusted.
Host processes running as the same user must not concurrently mutate bound paths,
private state, or the runtime installation. This is not a multi-tenant service or a
boundary against a compromised kernel, administrator, or cooperating malicious host
process. It has not received an independent security audit.

The supported operation is a computation over explicit read grants, followed by
publication of one explicitly authorized regular file or stdout stream. There is no
general host-administration mode.

## Separate authority from code

`Request.bind` owns read grants and output permission. A recipe contains no write
grants, directory authorization, or "safe" flag. JSON Schema rejects invented fields.
Schema/AST and command-option checks are structural checks only: a program that
passes them still runs inside OS confinement. Python's `exec` is used only inside the
confined worker; restricted builtins and import filtering are not the security
mechanism.

`--overwrite` permits replacement only of the declared output. `--modify` must name
the exact file supplied as both a direct input and the output. All generated writes
are staged; the worker never receives write access to the original destination.
Direct input/output aliases, final-component symlinks, special files, and multiply
linked outputs receive explicit checks. Output-directory grants are not supported.

Shell recipes contain argument-vector stages, not arbitrary shell source. Literal
arguments and whole input/parameter/time references are bound and quoted by trusted
code. A fixed renderer constructs the actual Bash pipeline and handles each stage's
exit status. Known output-writing/recursive-execution flags are rejected, but native
expressions and parsers remain untrusted; the catalog is not a security boundary.
Native stages may create confined subprocesses. Python fallback recipes retain their
more restrictive single-process policy.

Discovery checks executable metadata without invoking a recipe-selected binary.
Selected paths and metadata identities are frozen in a plan and rechecked for
preflight, fixtures, and execution. This is not authenticity verification or a
cryptographic runtime lock. Installed tools and their libraries are trusted runtime.

The library is not an adversarial API against its own Python caller. A Python caller
already has its interpreter's authority, can monkeypatch code, and can call unrelated
OS functions. CLI/model content cannot inject a replacement sandbox implementation.
There is no production "test", "trusted", "unrestricted", or "unsafe" backend.

## Linux adapter

Bubblewrap constructs a new mount/PID/IPC/network/user namespace with a read-only
root. Only declared inputs and runtime dependencies are mounted read-only. There is
one writable host file: the worker's staged output. Scratch is a private size-limited
tmpfs. No host home, complete working directory, `/proc`, D-Bus/SSH/container socket,
or ordinary environment is automatically mounted/passed. A user can grant broad
reads by explicitly choosing a broad input directory.

The standalone bootstrap loads a native libseccomp positive syscall allow-list
before compiling or executing recipe code. The Python policy denies process/thread
creation and exec. Native pipelines use a separate policy allowing fork/exec/wait,
pipes, and required native-runtime operations while retaining the namespace,
filesystem, and network boundary. The filter is inherited by descendants.

Networking, ptrace, mount, kernel IPC, io_uring, chmod/chown, explicit timestamp
mutation, and new hard/symbolic links remain denied. Shell clone flags are filtered
to prohibit namespace changes and parent detachment; clone3 returns ENOSYS so a
supported libc can use its inspectable clone path. Session/process-group detachment
remains denied. This shell clone policy supports x86_64 and aarch64 Linux. The filter
cannot be removed by changing Python builtins or reloading a module.

Read-only runtime library trees are a deliberate exception to caller data reads.
They include system libraries and the interpreter's library directories, with known
site-package locations masked. Runtime trees must not contain private application
data. `-S -s -P -B` plus a constructed environment prevents normal site/user startup
and input-directory imports, while allowing a fixed `PYTHONHASHSEED`. This is not a
promise that hostile code cannot locate a library already in the trusted trees.

Native pipelines additionally expose exact selected executables and their symlink
targets. Linux dependencies outside the supported runtime roots fail inside the
boundary; discovery does not add a whole home or non-system installation tree. The
initial Bash process disables startup files and receives no BASH_ENV, ENV, exported
functions, loader overrides, or model credentials. Native locale is C.

Missing Bubblewrap features, namespace restrictions, a missing libseccomp library,
or a rejected filter cause failure. They never remove a restriction to get a run
through. The deployment must provide a Bubblewrap version supporting `--size`.

## macOS adapter and its lifecycle limit

`sandbox-exec` installs a default-deny Seatbelt profile. Read grants cover runtime
libraries, bootstrap files, explicit inputs, scratch, and the worker's staged result.
Read-only parent metadata supports traversal; this is not authority to list parents
or read sibling file contents. Known site-package locations are denied.

The coordinator canonicalizes each declared macOS input path before producing
both the Seatbelt profile and the worker configuration. This avoids mismatched
`/var` versus `/private/var` names. Ancestor and symlink-node traversal is
metadata/existence-only; no recursive read of the parent temp directory or home
is introduced. The same rules apply when the caller chooses a private workspace
with `--temporary-directory` or `ANY_TEMPORARY_DIRECTORY`. Workspaces still use
owner-only randomized per-run directories and cannot sit inside a declared
input-directory grant. The environment setting is not forwarded to the worker.

The profile does not permit network access, Unix sockets, Mach service lookup, or
Apple Events. Python recipes deny process forking and permit only the interpreter
executable. Native pipelines allow process forking and the selected executables;
descendants inherit the same profile. Exact non-system dylib dependencies are read
from installed runtime binaries with the fixed `/usr/bin/otool -l` reader, without
executing candidate tools or granting an entire installation directory. Missing or
unresolvable dependencies fail explicitly.

Unlike Linux, this adapter does not provide a PID namespace or a syscall filter
preventing every process-group/session change. A rapidly detaching descendant may
escape supervisor discovery and survive the original leader. Cleanup is **best
effort on macOS**, not a guarantee that every native descendant has stopped. Such a
process still inherits the filesystem/network profile and hard per-process limits.
It cannot modify the separate coordinator-owned result snapshot used for validation
and publication. Do not use this prototype as a hostile-process containment service.
macOS threads are likewise not independently prohibited; they share the profile.

Scratch writes are private. The output receives write-data permission, not a general
write grant on its parent. A bootstrap hard-link canary must be denied before user
code runs, preventing an unsupported policy from allowing a link from a read-only
input into writable scratch. Process/network/read/write canaries must pass as well.
Native mode also checks inherited denials in a freshly executed child. A failed
canary is a failed deployment, not a reason to weaken the profile.

This adapter requires real OS/interpreter testing, particularly for Python and
Homebrew distributions with different dynamic-library layouts. Policy rendering
and Linux tests do not validate Seatbelt behavior. Keep macOS integration gates.

## Limits, shutdown, and stable results

The bootstrap sets hard CPU, open-file, core-dump, and per-file size limits before
running a recipe. Linux additionally gets an address-space limit and a hard scratch
tmpfs capacity. Native tools that reserve large virtual spaces can need an explicit
higher memory budget. Limits are never relaxed automatically.

A supervisor bounds diagnostics and wall time, monitors resident memory and process
count, and kills the original process group and observed descendants before releasing
a result. Bubblewrap's PID namespace adds a Linux teardown boundary. The default
64-process cap is supervised, not an aggregate kernel quota. CPU/file/address-space
limits apply per process; a whole native job is not a cgroup-backed resource domain.

On macOS, resident-memory and aggregate scratch checks are also polling controls.
A process can overshoot between samples, directory accounting does not reliably
count unlinked-but-open files, and a detached unobserved descendant can survive
cleanup. Hard per-file and open-file limits still apply. These controls reduce
accidents; they are not a full denial-of-service guarantee. Use hosts that can
tolerate temporary resource pressure, not hostile multi-tenant deployments.

After the supervised run, trusted code makes a bounded copy into a fresh private
inode outside all worker grants. It checks type, link count, identity, size, and
modification metadata while copying. Only that completed snapshot is exposed to
validators and the publisher. A worker retaining the original staged descriptor
cannot change the bytes being validated or later published. Private workspaces are
rejected beneath input-directory grants or runtime read trees.

The worker starts with null stdin, diagnostic pipes, `close_fds=True`, and a
constructed environment. Native final stdout goes to the staged file. No live
Sheetbend client, model credential, or connection handle is passed to the worker.

## Planning, inspection, preflight, and tests

Input inspection uses bounded ordinary reads, never imports/executables from an
input directory. It sends only permitted bounded request context to a selected
model source. Input previews are framed as data, not instructions; this framing
is not relied upon for execution authority.

Bounded planning may look up command metadata and repair invalid candidates using
the same selected Sheetbend connection. It never runs the candidate. Transport or
source-selection failures stop the invocation. An accepted cached candidate does
not silently mutate after an execution failure.

`--plan-only` checks input-root readability and prepares/displays a candidate. It
performs no sandbox or output-parent write probe and has no execution/publication
path. `--dry-run` additionally opens declared input roots inside the actual boundary,
checks trusted canaries and a fixed Bash no-op for native mode, and probes output
publication primitives using private parent-directory entries. It never runs the
candidate or its fixtures, nor opens the final output for writing.

Preflight establishes only present access to those roots and private probe entries.
It does not prove every directory descendant is readable, that actual-target ACLs,
sticky-directory/immutable restrictions permit replacement, or that capacity and
permissions will remain unchanged. Publication must still perform its actual checks.

Synthetic fixtures use the production sandbox. Fixture writers create regular files
in private directories and reject traversal and duplicate paths. Expected failures
accept only the worker's ordinary program-error exit, not a missing boundary,
timeout, or killed process. Python recipes require a successful fixture. Shell
fixtures are optional; checking a zero-fixture shell recipe runs trusted boundary
preflight and reports zero tests. Model-authored code/tests may share a mistake.

Linux shell input paths are private `/inputs/...` bindings. Commands that print
filenames need deliberate relative/display labels; those private paths are not
reusable host filenames. The planner is instructed to choose a suitable native
format or Python fallback. There is no blanket rewriting of output contents.

## Validation and publication

Output validation consumes the coordinator-owned snapshot, checks a bounded,
singly linked regular file, its declared text/JSON/table format, and required
columns. These checks do not prove row selection, identity preservation, or
scientific interpretation. Operation-specific semantic review is still necessary.

The publisher opens the authorized parent directory and checks its identity. New
outputs use a same-filesystem link operation that fails when a target already exists;
there is no racy "exists, then overwrite" fallback. Unsupported filesystems fail
instead of falling back to an unsafe publication method.

Explicit replacements use a staged file and one atomic replacement operation. Target
state is rechecked, but this is not a general compare-and-swap against concurrent
host writers. The caller must arrange exclusive ownership or external locking when
needed. Ordinary permission bits are retained; ACLs, extended attributes, ownership
differences, and hard-link relationships are not recreated.

Only one task file is published. There is no multi-file transaction or universal
rollback claim. Publication data is fsynced before placing the final entry, but this
is not a complete power-loss durability protocol for every filesystem. Automatic
read access may still affect filesystem access-time metadata.

All task stdout is buffered until validation succeeds. After bytes enter a pipe,
they cannot be recalled. No automatic retry follows a broken pipe. External shell
redirection is owned by the calling shell, not by `any`.

Run-receipt storage occurs after publication. If it fails, the error explicitly says
that output was published. Cache bookkeeping failures must not imply rollback.

## Cache, diagnostics, and reproducibility

Recipes are canonical JSON with content-derived identifiers. Digest verification
detects corruption, not authenticity. Qualification is bound to the recipe, selected
tool metadata, and observed runtime fingerprint, and never grants permissions. Every
invocation rechecks its inputs/destination and applies confinement again.

The cache contains plaintext generated code/descriptions, actual payloads, and run
metadata. Raw previews/prompts are not separately retained, but model output may
repeat their content. `--no-cache` disables `any` state, not the selected model's data
handling or Sheetbend's separate runtime ledger. Successful history is bounded to
100 receipts; it is not a durable audit trail.

`-vv` logs workflow/runtime details; `-vvv` may show bounded rejected model output,
selected provider error fields, and exception traces. Headers and complete provider
requests are not dumped. Recognizable credentials are redacted and terminal controls
escaped; arbitrary input data can still appear. Inspect logs before sharing them.

A runtime fingerprint and fixed defaults are not a hermetic lock. Libraries,
external host writers, nondeterministic code, and unrecorded state can affect results.
Input metadata is not a content checksum or recursive snapshot. Instructions to use
`context['now']`, ordered iteration, and parameters improve reproducibility without
formally proving it.

## References

The project owns its wrapper/policy code and does not vendor either native sandbox.

- Bubblewrap security model: https://github.com/containers/bubblewrap
- Bubblewrap options: https://github.com/containers/bubblewrap/blob/main/bwrap.xml
- libseccomp API: https://libseccomp.readthedocs.io/en/latest/
- A practical Seatbelt implementation: https://github.com/anthropics/sandbox-runtime
- Python resource limits: https://docs.python.org/3/library/resource.html
- Python subprocess considerations: https://docs.python.org/3/library/subprocess.html#security-considerations
- Python exec limitations: https://docs.python.org/3/library/functions.html#exec

The supplied Sheetbend source is the authority for its integration API. Its connection
client is reused, not replaced by an endpoint client inferred from these references.
