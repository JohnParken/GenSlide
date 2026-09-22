# Assistant execution workspace implementation record

Implemented: assistant turn contract; model decision followed by complete Skill-guided composition;
immutable request attachment scope and execution descriptor; trusted claim snapshot restoration;
lifecycle-aware commit; temporary inputs/scratch/outputs, disk monitoring, POSIX active locks and
orphan sweeping. Direct drafting requires no outline confirmation. Scoped revisions merge only
the requested sections. Public request schemas reject old operation/body/snapshot fields.

Current boundary: only execution context and temporary workspace. Persistent user authoring spaces,
production BFF/database/storage services, artifact revision catalogs and Vue integration are deferred.
TDSQL MariaDB 10.3 DDL is a design draft, not an applied or database-validated migration.
The memory-only mock demonstrates protocol behavior, not production durability or physical cleanup.

## Validation

- Root: `.venv/bin/python -m pytest tests -q` — 46 passed, two dependency deprecation warnings.
- Service: `backend/.venv/bin/pytest -q backend/tests` — 114 passed.
- `git diff --check` — passed.
- Real-model, production BFF/TDSQL and deployed Kubernetes checks were not run.
- `make run` opens the new server-side assistant demo; `service_chat.py` remains a legacy reference.

## Subagent execution record

Dispatches and follow-ups, including incomplete attempts:

| Agent | Work and outcome |
| --- | --- |
| luna__assistant_core / Luna Worker | Initial core implementation dispatch interrupted; resumed for assistant/workspace integration, then handed partial code back without claiming validation. Main agent completed integration. |
| luna__workspace / Luna Medium | Initial workspace implementation (4 tests); safety correction (7 tests); fallback verification after unavailable Spark (11 tests); additional workspace/old-test migration follow-up (12 workspace tests, migration blocked by in-progress core). |
| spark__workspace_validation | Launch rejected: model unavailable. No execution or result. Subsequent testing used Luna under the updated AGENTS instructions. |
| luna__root_tests / Luna Low | Initial root run: 46 passed. Final regression follow-up ran service pytest against the wrong root and failed collection; corrected explicit service path follow-up passed 111 tests at that point. Main agent subsequently added three cases and verified 114. |
| luna__runtime_tests / Luna Low | Initial runtime/API migration exposed missing cross-instance restore; follow-ups updated lifecycle fixture and decision/compose fixtures. Final run: 11 passed. |
| luna__turn_tests / Luna Low | Initial five-file workflow/Skill migration: 42 passed. Follow-up removed hard-coded built-in Skill versions and added policy/metadata cases: 46 passed. |

## Main-agent work

Owned boundaries and authorization review, completed the partial core, introduced the two-phase
Skill decision/composition path, strict requirement evidence and current-body hash checks, restored
trusted snapshots across instances, added immutable execution context and quota cancellation,
protected Skill reload, wired periodic sweeping and deployment limits, fixed mock lifecycle/replay
barriers, added development/public clients and demo, synchronized schemas, updated documentation,
reviewed worker diffs and ran the final complete regression. No commits or deployment were made.
