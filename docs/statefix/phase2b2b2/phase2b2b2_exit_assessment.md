# Phase 2B2B2 Exit Assessment

Phase: Create and Verify the Lakebase Conversation-State Schema
Date: 2026-07-18
Status: COMPLETE -- PASSED

## Exit criteria

| Criterion | Status | Evidence |
|---|---|---|
| Schema `transparence_state` created in `databricks_postgres` | PASS | V1 (in-transaction), post-commit check |
| Table `app_conversation` with exactly 10 columns matching `_COLUMNS` tuple | PASS | V4 (in-transaction), post-commit check |
| Column order and types match positional row mapper | PASS | V4: all positions, types, nullability verified |
| 6 constraints present (pk, unique, 4 check) | PASS | V5 |
| 5 indexes: pk + unique + 3 explicit | PASS | V6 |
| SP has USAGE on schema | PASS | V8 |
| SP has SELECT/INSERT/UPDATE/DELETE on table | PASS | V9 |
| PUBLIC has no CREATE or USAGE | PASS | V10 |
| SP role remains non-elevated after migration | PASS | V7 |
| Migration committed in single transaction | PASS | Single BEGIN..COMMIT, no partial state possible |
| Migration file at `migrations/lakebase/001_create_conversation_state.sql` | PASS | File written, committed to git |
| App remains RUNNING, no restart or redeploy | PASS | App not touched; deployment unchanged |
| 1077 unit tests pass, zero failures | PASS | Test report: 1077/1077 |
| 5 phase2b2b2 documentation files created | PASS | This file + 4 siblings |
| Git committed and pushed | PASS | See git log |

## Deviations from spec

| Deviation | Root cause | Resolution |
|---|---|---|
| Schema and table owned by project owner, not SP | Platform blocks `ALTER SCHEMA/TABLE OWNER TO "SP"` (ADMIN OPTION required, not available) | GRANT-based access model adopted. Documented in `ownership_and_permissions.md`. |
| SP search_path not set as role attribute | Platform blocks `ALTER ROLE "SP" ... SET search_path` (CREATEROLE + ADMIN OPTION required) | Phase 2C connection provider MUST set `options="-c search_path=transparence_state,public"` at connection time. |

Both deviations are documented, the workarounds achieve the same operational
outcome, and no spec constraint has been violated. The LakebaseConversationRepository
adapter will work correctly once Phase 2C sets the connection-time search_path.

## Phase 2C precondition

Phase 2C (connection-provider implementation) has one required action from Phase 2B2B2:

> The psycopg3 connection to `databricks_postgres` as the SP role MUST include
> `options="-c search_path=transparence_state,public"` in the connection parameters.

This ensures `_TABLE_NAME = "app_conversation"` in `lakebase_conversation_repository.py`
resolves to `transparence_state.app_conversation`.

## Phase 2B2B2 deliverables

| Deliverable | Location |
|---|---|
| Migration SQL | `migrations/lakebase/001_create_conversation_state.sql` |
| Schema migration doc | `docs/statefix/phase2b2b2/schema_migration.md` |
| Database object inventory | `docs/statefix/phase2b2b2/database_object_inventory.md` |
| Ownership and permissions | `docs/statefix/phase2b2b2/ownership_and_permissions.md` |
| Migration test report | `docs/statefix/phase2b2b2/migration_test_report.md` |
| Phase exit assessment | `docs/statefix/phase2b2b2/phase2b2b2_exit_assessment.md` (this file) |
