# TDSQL MariaDB 10.3 BFF persistence contract

This document specifies the persistence boundary expected by GenSlide. The production BFF is
implemented outside this repository. GenSlide never connects to TDSQL or object storage directly.

Status: future user authoring-space design, not a deployed migration. This iteration implements
only the action execution context and disposable local workspace. Validate the DDL on the actual
TDSQL MariaDB 10.3 deployment before use. Identity/idempotency columns require case-sensitive
collation. This draft does not demonstrate database compatibility or production readiness.

## Compatibility rules

- Use InnoDB, `utf8mb4`, UTC `DATETIME(6)`, and application-generated `CHAR(26)` or `CHAR(36)` IDs.
- Store statuses in `VARCHAR`; validate them in application code.
- Store schema-validated JSON as `LONGTEXT`. Do not depend on JSON indexes or MySQL 8 functions.
- Do not use PostgreSQL-specific UUID/JSONB/array types, partial indexes, `RETURNING`, advisory locks,
  database triggers, stored procedures, or `SKIP LOCKED`.
- Authorization, action arbitration, result commits, and deletion barriers must use the primary.

## Reference DDL

The BFF may add audit columns, but must preserve the keys and concurrency fields below.

```sql
CREATE TABLE sessions (
  id CHAR(26) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  owner_id VARCHAR(128) NOT NULL,
  status VARCHAR(24) NOT NULL,
  session_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
  lifecycle_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
  runtime_epoch VARCHAR(128) NOT NULL,
  current_artifact_id CHAR(26) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  deleted_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  KEY ix_sessions_owner (tenant_id, owner_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE actions (
  id CHAR(26) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  user_id VARCHAR(128) NOT NULL,
  session_id CHAR(26) NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  request_fingerprint CHAR(64) NOT NULL,
  status VARCHAR(24) NOT NULL,
  base_session_version BIGINT UNSIGNED NOT NULL,
  base_lifecycle_version BIGINT UNSIGNED NOT NULL,
  execution_instance_id VARCHAR(128) NULL,
  execution_token_hash CHAR(64) NULL,
  skill_id VARCHAR(128) NULL,
  skill_version VARCHAR(64) NULL,
  lease_expires_at DATETIME(6) NULL,
  deadline_at DATETIME(6) NOT NULL,
  result_json LONGTEXT NULL,
  receipt_json LONGTEXT NULL,
  error_code VARCHAR(64) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_action_idempotency (tenant_id, user_id, session_id, idempotency_key),
  KEY ix_actions_session_status (session_id, status, updated_at),
  KEY ix_actions_lease (status, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE messages (
  id CHAR(26) NOT NULL,
  session_id CHAR(26) NOT NULL,
  action_id CHAR(26) NULL,
  role VARCHAR(24) NOT NULL,
  effect VARCHAR(24) NULL,
  body LONGTEXT NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_messages_session (session_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE artifacts (
  id CHAR(26) NOT NULL,
  session_id CHAR(26) NOT NULL,
  target_kind VARCHAR(24) NOT NULL,
  current_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_artifacts_session (session_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE artifact_revisions (
  artifact_id CHAR(26) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  action_id CHAR(26) NOT NULL,
  content_json LONGTEXT NOT NULL,
  content_hash CHAR(64) NOT NULL,
  skill_id VARCHAR(128) NOT NULL,
  skill_version VARCHAR(64) NOT NULL,
  skill_hash CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (artifact_id, revision),
  UNIQUE KEY uq_revision_action (action_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE session_snapshots (
  session_id CHAR(26) NOT NULL,
  session_version BIGINT UNSIGNED NOT NULL,
  schema_version INT UNSIGNED NOT NULL,
  snapshot_json LONGTEXT NOT NULL,
  snapshot_hash CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (session_id),
  UNIQUE KEY uq_snapshot_version (session_id, session_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE files (
  id CHAR(26) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  session_id CHAR(26) NOT NULL,
  action_id CHAR(26) NULL,
  artifact_id CHAR(26) NULL,
  artifact_revision BIGINT UNSIGNED NULL,
  artifact_slot VARCHAR(64) NULL,
  kind VARCHAR(32) NOT NULL,
  status VARCHAR(24) NOT NULL,
  filename VARCHAR(512) NOT NULL,
  content_type VARCHAR(255) NOT NULL,
  object_key VARCHAR(1024) NOT NULL,
  storage_version_id VARCHAR(512) NULL,
  size_bytes BIGINT UNSIGNED NULL,
  sha256 CHAR(64) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  deleted_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_file_action_slot (tenant_id, action_id, artifact_slot),
  KEY ix_files_session_status (session_id, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE session_files (
  session_id CHAR(26) NOT NULL,
  file_id CHAR(26) NOT NULL,
  attached TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(6) NOT NULL,
  detached_at DATETIME(6) NULL,
  PRIMARY KEY (session_id, file_id),
  KEY ix_session_files_active (session_id, attached)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE outbox_events (
  id CHAR(26) NOT NULL,
  event_key VARCHAR(255) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  aggregate_id CHAR(26) NOT NULL,
  status VARCHAR(24) NOT NULL,
  payload_json LONGTEXT NOT NULL,
  worker_id VARCHAR(128) NULL,
  lease_expires_at DATETIME(6) NULL,
  attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  next_attempt_at DATETIME(6) NOT NULL,
  last_error VARCHAR(1000) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_outbox_event_key (event_key),
  KEY ix_outbox_claim (status, next_attempt_at, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE cleanup_items (
  id CHAR(26) NOT NULL,
  event_id CHAR(26) NOT NULL,
  file_id CHAR(26) NULL,
  object_key VARCHAR(1024) NOT NULL,
  storage_version_id VARCHAR(512) NULL,
  status VARCHAR(24) NOT NULL,
  attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(1000) NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  object_identity_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  UNIQUE KEY uq_cleanup_object (event_id, object_identity_hash),
  KEY ix_cleanup_event_status (event_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

## Atomic result commit

In one short primary transaction, lock the session row and then the action row in that order.
Verify `active`, both expected versions, execution ownership, token hash, and lease. Insert the
message and optional artifact revision, publish only the action's staged files, replace the current
snapshot, increment `session_version`, and persist the idempotent receipt. Model calls, rendering,
object transfers, and cache invalidation are never performed while holding these locks.

## Deletion barrier

Deleting a session locks the session row, changes `active` to `deleting`, increments
`lifecycle_version`, closes unfinished actions, materializes cleanup items, and writes one outbox
event in the same transaction. From that commit onward, downloads, receipt reads, renewals, uploads,
and result commits must fail closed. The object inventory is retained until every cleanup item is
confirmed deleted.

Compute `object_identity_hash` from canonical JSON containing storage namespace, object key and
nullable storage version ID. Do not concatenate ambiguous strings.

## Outbox claiming without SKIP LOCKED

Workers claim one candidate in a short transaction using `SELECT ... FOR UPDATE`, followed by a
conditional update from `pending` (or an expired `processing` lease) to `processing`. The transaction
commits before external deletion begins. Object-not-found is success; transient failure increments
the attempt count and schedules exponential backoff. Every cleanup operation must be idempotent.

Use a monotonically increasing claim generation to fence completion/renewal by an expired worker.
Lease expiry does not stop that worker from performing external I/O: retries must delete the same
immutable object identity safely, and must never target an overwritten/shared object.
