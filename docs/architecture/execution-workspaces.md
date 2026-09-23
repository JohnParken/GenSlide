# Three workspace layers

GenSlide distinguishes three lifetimes:

| Layer | Owner | Status |
| --- | --- | --- |
| User authoring space | BFF, TDSQL and private object storage | Deferred; no new project/session persistence service in this iteration |
| Execution context | GenSlide, constrained by an authoritative BFF claim | One action; immutable authorization/version inputs |
| Temporary execution directory | GenSlide local storage | One execution instance; disposable inputs, scratch and outputs |

## Execution context

Resolve identity, action ownership, session/lifecycle versions, deadline and authorized file IDs
before calling the model. Fix the chosen Skill content/version/hash for that turn. UI selection
changes and Skill reloads apply only to later turns. Model output cannot expand file access,
change identity or extend the lease. Materials and current content are data, not tool instructions.

The context may load a schema-validated snapshot from the BFF claim. Local state is only a cache.
Do not accept a browser-provided snapshot or silently reset an incompatible snapshot. Persistent
user spaces, shared materials, historical editing and database migrations remain outside this
iteration. A dev mock cannot establish production authorization or durability guarantees.

The committed snapshot deliberately includes the current draft body for subsequent editing.
The lightweight memory budget excludes that body; a separate snapshot budget applies. Strict
schema fields prevent arbitrary extra fields, not semantic information flow: a string can still
contain a path or copied material. Never persist raw attachment parses, tool transcripts or local
paths as incidental execution state. Fresh execution context does not mean discarding the draft.

## Temporary execution directory

Settings: `GENSLIDE_WORKSPACE_ROOT` selects a dedicated local root (not a general temporary
directory); `GENSLIDE_WORKSPACE_MAX_BYTES` checks total managed-root usage;
`GENSLIDE_WORKSPACE_REQUEST_MAX_BYTES` checks each execution directory (default 128 MiB,
including manifests, inputs and intermediate/output files);
`GENSLIDE_WORKSPACE_MIN_FREE_BYTES` reserves disk space; `GENSLIDE_WORKSPACE_STALE_SECONDS`
controls orphan reclamation age. The ASGI lifespan sweeps at startup and at most 60-second
intervals. Nonblocking file locks protect active workspaces across local processes. This manager
requires a POSIX host (Linux/macOS); run the service in Linux/WSL on Windows.

The configured local root contains random per-execution directories with `inputs/`, `scratch/`,
`outputs/` and a metadata-only manifest. No service tokens, material text or model credentials
belong in the manifest. User filenames and IDs never determine directory traversal paths.

Check disk usage at stage boundaries, every 0.5 seconds during execution and before upload.
Per-execution overflow returns `WORKSPACE_REQUEST_CAPACITY` (507); root overflow or insufficient
free space returns `WORKSPACE_CAPACITY` (507). These checks can overshoot; they are not an
operating-system hard quota. Deployments must also set volume and ephemeral-storage limits and
reserve free disk space. Strict per-request hard isolation requires filesystem quotas or separate
execution containers; it is not provided by this in-process runtime. The sum of attachment and
artifact limits is not a disk upper bound: intermediates and orphan directories also consume space.
A temporary directory is not a security sandbox for arbitrary Skill scripts.
Skills in this version are instructions, not executable programs.

Stop and await writers before cleanup. Clean normal completion, failure, cancellation and timeout.
Log failed cleanup and retain its manifest for subsequent reclamation. Sweeping may only inspect
managed directories and must skip active leases/locks and symbolic links. Never sweep `/tmp`
generically or delete a directory merely because its name resembles an action ID.

The deployment template mounts a size-limited emptyDir at the configured workspace root's parent.
SIGKILL cannot run finally: a surviving/restarted service reclaims unlocked stale directories after
the configured age. Container restart retains emptyDir; Pod deletion removes it. Persistent volumes
require the same safe orphan sweep and an independently enforced storage budget.

## Recovery and cancellation

An API restart can restore only committed state from the BFF. Temporary files do not checkpoint
partially generated text. Client disconnect retains the existing cancellation semantics; clients
must query the same action after reconnecting and never automatically re-run generation under a
new action ID. Unknown commit outcomes require authoritative BFF reconciliation.

Cleanup runs in a retained, shielded task and is joined even after repeated cancellation. The
pipeline (including CPU child termination/reaping) must finish before directory cleanup and slot
release. Release marks success under the admission lock only after ownership has been removed.
Expired engine caches are deleted outside that lock with a control timeout and a per-key tombstone;
unrelated admissions proceed, while the same key is rejected until deletion ends. Engine adapters
must cooperate with cancellation and must not return from delete while detached writers remain.

Attachment suffixes and default input size have one policy definition. The configured input/text
limits are passed to parsing subprocesses; dedicated overflow errors remain 413 rather than generic
invalid-file errors. DOCX expanded size and PDF page count are separate parser safety limits.

## Validation boundaries

Exercise quota overflow, insufficient free space, creation failure after claim, symlink rejection,
active-directory sweep exclusion, stale-directory reclamation, failed cleanup retry, cancellation,
and admission release. Database persistence, real TDSQL transactions and production BFF deletion
workers require separate integration acceptance when the user-space layer is implemented.
