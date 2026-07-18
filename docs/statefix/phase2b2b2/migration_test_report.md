# Migration Test Report

Phase: 2B2B2
Date: 2026-07-18

## Migration execution

- Transaction opened: 2026-07-18
- Execution identity: `lokesh.choraria@te.com`
- Endpoint: `ep-withered-king-d257e0k1.database.us-east-1.cloud.databricks.com`
- Database: `databricks_postgres`
- psycopg version: 3.3.4
- SDK version: databricks-sdk 0.121.0

## In-transaction verification results

All 11 checks were run INSIDE the transaction before COMMIT.

| Check | Description | Result |
|---|---|---|
| V1 | Schema `transparence_state` exists in `information_schema.schemata` | PASS: owner=`lokesh.choraria@te.com` |
| V2 | Table `transparence_state.app_conversation` exists | PASS |
| V3 | Table row count = 0 | PASS: row_count=0 |
| V4 | Exactly 10 columns, correct order/type/nullability | PASS |
| V5 | All 6 constraints present (pk, unique, 4 check) | PASS |
| V6 | Exactly 5 indexes (3 explicit + pk + unique) | PASS |
| V7 | SP role non-superuser, no CREATEDB, no CREATEROLE | PASS |
| V8 | SP has USAGE on schema | PASS |
| V9 | SP has SELECT/INSERT/UPDATE/DELETE on table | PASS |
| V10 | PUBLIC has no CREATE or USAGE on schema | PASS |
| V11 | Schema owner is interactive project owner | PASS: owner=`lokesh.choraria@te.com` |

Commit: SUCCESS.

## Post-commit verification (fresh connection)

10 independent checks on a new OAuth connection after COMMIT.

| Check | Result |
|---|---|
| Schema exists and owner correct | PASS |
| Table exists and owner correct | PASS |
| Row count = 0 | PASS |
| 10 columns in correct order/type/nullability | PASS |
| 6 constraints present | PASS |
| 5 indexes present | PASS |
| SP has USAGE on schema + SELECT/INSERT/UPDATE/DELETE on table | PASS |
| PUBLIC has no CREATE or USAGE on schema | PASS |
| SP role remains non-elevated | PASS |
| No unrelated schemas created | PASS |

## Application unit test suite

Executed: 2026-07-18
Method: pytest, two-run strategy (PYTHONDONTWRITEBYTECODE=1, --import-mode=importlib)

| Run | Files | Passed | Failed | Errors |
|---|---|---|---|---|
| Main suite (27 files) | All except test_*_conversation_repository.py | 911 | 0 | 0 |
| Isolated (2 files) | test_conversation_repository.py + test_lakebase_conversation_repository.py | 166 | 0 | 0 |
| **TOTAL** | **29 files** | **1077** | **0** | **0** |

Note: The two-run strategy was required due to a pre-existing namespace collision
in the test collection phase when all 29 files are collected simultaneously with
`--import-mode=importlib`. The collision is caused by two `app/` directories in
the workspace (REPO/app/ and transparence_app/app/) competing for the `app`
package namespace. This is a test infrastructure issue unrelated to Phase 2B2B2
changes. When run individually or in isolation both files pass.

Excluded (live smoke tests):
- `test_genie_live_smoke.py`
- `test_genie_integration_smoke.py`
- `test_delta_state_live_smoke.py`
- `test_new_pipeline_live_smoke.py`

## Summary

Migration applied cleanly. All in-transaction and post-commit checks passed.
All 1077 unit tests pass. Phase 2B2B2 is ready for exit.
