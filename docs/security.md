# Execution and publication contract

## Threat model

Generated code, input contents/names, imported recipe files, and cached code are
untrusted. The caller, `any` coordinator, installed interpreter/runtime libraries,
Sheetbend configuration, native sandbox implementation, and OS/kernel are trusted.
Host processes running as the same user must not concurrently mutate bound paths,
private state, or the runtime installation. This is not a multi-tenant service or a
boundary against a compromised kernel, administrator, or cooperating malicious host
process. It has not received an independent security audit.

The promised first-release operation is a computation over explicit read grants,
followed by publication of one explicitly authorized regular file or stdout stream.
There is no general host-administration mode.

## Separate authority from code

`Request.bind` owns read grants and output permission. A recipe contains no write
grants, directory authorization, or "safe" flag. JSON Schema rejects invented fields.
Schema/AST checks are structural checks only: a program that passes them still runs
inside OS confinement. Python's `exec` is used **only inside the confined worker**;
restricted builtins and import filtering are not the security mechanism.

`--overwrite` permits replacement only of the declared output. `--modify` must name
the exact file supplied as both a direct input and the output. All generated writes
are staged; the worker never receives write access to the original destination.
Direct input/output aliases, final-component symlinks, special files, and multiply
linked outputs receive explicit checks. Output-directory grants are not supported.

The library is not an adversarial API against its own Python caller. A Python caller
already has its interpreter's authority, can monkeypatch code, and can call unrelated
OS functions. CLI/model content cannot inject a replacement sandbox implementation.
There is no production "test", "trusted", "unrestricted", or "unsafe" backend.

## Linux adapter

Bubblewrap constructs a new mount/PID/IPC/network/user namespace with a read-only
root. Only declared inputs and runtime dependencies are mounted read-only. There is
one writable host file: the staged output. Scratch is a private size-limited tmpfs.
No host home, complete working directory, `/proc`, D-Bus/SSH/container socket, or
ordinary environment is automatically mounted/passed. A user can of course grant
broad reads by explicitly choosing a broad input directory.

The standalone bootstrap loads a native libseccomp **positive syscall allow-list**
before compiling or executing recipe code. Networking, process/thread creation,
exec, ptrace, mount, signaling other processes, kernel IPC, io_uring, chmod/chown,
explicit timestamp mutation, and new hard/symbolic links are not on that list.
The filter cannot be removed by changing Python builtins or reloading a module.

Read-only runtime library trees are a deliberate exception to caller data reads.
They include system libraries and the interpreter's library directories, with known
site-package locations masked. Runtime trees must not contain private application
data. `-S -s -P -B` plus a small, constructed environment prevents normal site/user
startup and input-directory imports, while allowing a fixed `PYTHONHASHSEED`.
This is not a promise that a deliberately hostile program cannot locate any native
library already included in the trusted runtime trees.

Missing Bubblewrap features, namespace restrictions, a missing libseccomp library,
or a rejected filter cause failure. They never remove a restriction to get a run
through. The deployment must provide a Bubblewrap version supporting `--size`.

## macOS adapter

`sandbox-exec` installs a default-deny Seatbelt profile. Read grants cover runtime
libraries, the bootstrap files, explicit inputs, scratch, and the staged result.
Read-only parent metadata supports traversal; this is not authority to list parents
or read sibling file contents. Known site-package locations are denied.

The profile does not permit network access, Unix sockets, Mach service lookup,
Apple Events, or process forking. Only the interpreter executable is permitted for
initial execution. Re-exec of that interpreter retains the same OS profile; it does
not grant a different set of paths. Unlike Linux's filter, macOS threads are not
independently prohibited by a syscall filter; they share the process boundary.
Recipes are instructed not to create threads.

Scratch writes are private. The output receives write-data permission, not a general
write grant on its parent. A bootstrap hard-link canary must be denied before user
code runs, preventing an unsupported policy from silently allowing a link from a
read-only input into writable scratch. Process/network/read/write canaries must pass
as well. A failed canary is a failed deployment, not a reason to weaken the profile.

This adapter needs real OS/interpreter testing, particularly for Python distributions
with different dynamic-library layouts. Policy rendering tests alone do not validate
Seatbelt behavior. Keep macOS release integration gates enabled.

## Limits and shutdown

The bootstrap sets hard CPU, open-file, core-dump, and per-file size limits before
running a recipe. Linux additionally gets an address-space limit and a hard scratch
tmpfs capacity. A supervisor bounds diagnostics and wall time, monitors resident
memory, and cleans up the process group and observed descendants. Bubblewrap's PID
namespace provides an additional teardown boundary.

On macOS, resident-memory and aggregate scratch checks are polling controls, not
kernel quotas. A program can overshoot between samples, and directory-based scratch
accounting does not reliably account for unlinked-but-open files. Hard per-file and
open-file limits still apply. These controls reduce accidents; they are not a full
denial-of-service guarantee. Do not deploy this prototype for hostile multi-tenant
workloads or on a host that cannot tolerate temporary resource pressure.

The worker is launched with stdin connected to the null device, stdout/stderr pipes,
`close_fds=True`, and a constructed environment. It receives no model credentials or
connection handles. There is no live Sheetbend client in the worker.

## Inspection, tests, and output validation

Input inspection uses bounded ordinary reads, never imports/executables from an
input directory. It sends only the permitted bounded request context to a selected
model source. Input previews are explicitly framed as data, not instructions.
That framing is not relied upon for execution authority.

Synthetic fixture code uses the production sandbox. Fixture writers only create
regular files in private directories and reject path traversal or duplicate paths.
Expected failures only accept the worker's ordinary program-error exit, not a
missing sandbox, timeout, or killed process. At least one successful fixture is
required. Model-authored programs/tests may share an error; correctness is not proven.

After execution, output validation checks a bounded, singly linked regular file,
its declared text/JSON/table format, and required columns. These checks do not prove
row selection, identity preservation, or any scientific interpretation. Third-party
reviewed recipes and stronger operation-specific validation can be added deliberately
later without replacing the authority boundary.

## Publication

The publisher opens the authorized parent directory and checks its identity. New
outputs use a same-filesystem link operation that fails when a target already exists;
there is no racy "exists, then overwrite" fallback. Filesystems without the required
operation fail rather than falling back to an unsafe publication method.

Explicit replacements use a staged file and one atomic replacement operation. Target
state is rechecked, but this is **not** a general compare-and-swap against concurrent
host writers. A caller must arrange exclusive ownership or appropriate external
locking when needed. Ordinary permission bits are retained; ACLs, extended attributes,
ownership differences, and hard-link relationships are not recreated.

Only one task file is published. There is no multi-file transaction or universal
rollback claim. Publication data is fsynced before placing the final entry, but this
is not a complete power-loss durability protocol for every filesystem. Automatic
read access may still affect filesystem access-time metadata.

All stdout is buffered in the staged file until validation succeeds. After bytes
enter a pipe, they cannot be recalled. No automatic retry follows a broken pipe.
External shell redirection is owned by the calling shell, not by `any`.

Run-receipt storage occurs after publication. If it fails, the error explicitly says
that output **was published**. Cache bookkeeping failures must not be described as
though the task output rolled back.

## Cache trust and reproducibility

Recipes are canonical JSON with content-derived identifiers. Digest verification
detects corruption; it does not establish authenticity. Qualification is separately
bound to an observed runtime fingerprint and does not grant permissions. Every
invocation rechecks the bound inputs/destination and applies confinement again.

The cache contains plaintext generated code/descriptions and run metadata. We do
not separately retain raw previews/prompts, but model output may repeat their content.
Use synthetic data or `--no-cache` when appropriate; neither option substitutes for
a data-handling review of a nonlocal model endpoint.

A runtime fingerprint and fixed defaults are not a hermetic runtime lock. Libraries,
external host writers, nondeterministic code, and unrecorded state can affect results.
Input metadata is not a content checksum or a recursive snapshot. Model instructions
to use `context['now']`, ordered iteration, and parameters help reproducibility but
are not formally verified properties of arbitrary Python code.

## References

Implementation decisions were checked against these primary references; the project
owns its wrapper/policy code and does not vendor either sandbox implementation.

- Bubblewrap security model and limitations: https://github.com/containers/bubblewrap
- Bubblewrap command options: https://github.com/containers/bubblewrap/blob/main/bwrap.xml
- libseccomp API: https://libseccomp.readthedocs.io/en/latest/
- A practical Seatbelt policy implementation: https://github.com/anthropics/sandbox-runtime
- Python resource limits: https://docs.python.org/3/library/resource.html
- Python subprocess security considerations: https://docs.python.org/3/library/subprocess.html#security-considerations
- Python builtins warning about exec restrictions: https://docs.python.org/3/library/functions.html#exec

The supplied Sheetbend snapshot is the authority for its integration API, not a
replacement endpoint client inferred from these external references.
