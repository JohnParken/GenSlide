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

## Temporary execution directory

Settings: `GENSLIDE_WORKSPACE_ROOT` selects a dedicated local root (not a general temporary
directory); `GENSLIDE_WORKSPACE_MAX_BYTES` bounds total managed-root usage;
`GENSLIDE_WORKSPACE_MIN_FREE_BYTES` reserves disk space; `GENSLIDE_WORKSPACE_STALE_SECONDS`
controls orphan reclamation age. The ASGI lifespan sweeps at startup and at most 60-second
intervals. Nonblocking file locks protect active workspaces across local processes. This manager
requires a POSIX host (Linux/macOS); run the service in Linux/WSL on Windows.

The configured local root contains random per-execution directories with `inputs/`, `scratch/`,
`outputs/` and a metadata-only manifest. No service tokens, material text or model credentials
belong in the manifest. User filenames and IDs never determine directory traversal paths.

Check disk usage during execution and before upload. These checks detect exhaustion; they are not
an operating-system hard quota. Deployments must also set ephemeral-storage limits and reserve
free disk space. A temporary directory is not a security sandbox for arbitrary Skill scripts.
Skills in this version are instructions, not executable programs.

Stop and await writers before cleanup. Clean normal completion, failure, cancellation and timeout.
Log failed cleanup and retain its manifest for subsequent reclamation. Sweeping may only inspect
managed directories and must skip active leases/locks and symbolic links. Never sweep `/tmp`
generically or delete a directory merely because its name resembles an action ID.

## Recovery and cancellation

An API restart can restore only committed state from the BFF. Temporary files do not checkpoint
partially generated text. Client disconnect retains the existing cancellation semantics; clients
must query the same action after reconnecting and never automatically re-run generation under a
new action ID. Unknown commit outcomes require authoritative BFF reconciliation.

## Validation boundaries

Exercise quota overflow, insufficient free space, creation failure after claim, symlink rejection,
active-directory sweep exclusion, stale-directory reclamation, failed cleanup retry, cancellation,
and admission release. Database persistence, real TDSQL transactions and production BFF deletion
workers require separate integration acceptance when the user-space layer is implemented.
