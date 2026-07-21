# H1: Tests, Build/Migration Blockers, and Deployment

## Test Results

### New Test Files

| File | Tests | Result |
|---|---|---|
| `tests/test_message_repository.py` | 21 | 21 passed |
| `tests/test_lakebase_message_repository.py` | 47 | 47 passed |
| `tests/test_message_history_service.py` | 18 | 18 passed |
| `tests/test_conversation_history_route.py` | 13 | 13 passed |
| **Total new** | **99** | **99 passed** |

### Existing Regression

| Suite | Count | Result |
|---|---|---|
| Frontend persistence (.mjs) | 52 | 52 passed |
| Frontend reset (.mjs) | 49 | 49 passed |
| Full non-live Python (excl. 4 live smoke files) | 2451 | 2451 passed, 36 skipped (asyncio), 0 failed |

## Migration Deployment Procedure

**PRECONDITION**: Migration must be applied before enabling `ENABLE_MESSAGE_HISTORY`.

```sql
-- 1. Apply migration (idempotent)
\i migrations/lakebase/002_create_conversation_message.sql

-- 2. Verify table
SELECT column_name, data_type FROM information_schema.columns
WHERE table_schema = 'transparence_state'
  AND table_name = 'app_conversation_message'
ORDER BY ordinal_position;

-- 3. Verify indexes
SELECT indexname, indexdef FROM pg_indexes
WHERE tablename = 'app_conversation_message';

-- 4. Verify grants
SELECT grantee, privilege_type FROM information_schema.table_privileges
WHERE table_schema = 'transparence_state'
  AND table_name = 'app_conversation_message';
```

## Deployment Order

1. Apply migration 002 to the Lakebase database.
2. Verify table, indexes, and SP grants.
3. Deploy app with `ENABLE_MESSAGE_HISTORY=true`.
4. Verify history API with a test user session.
5. Roll back: set `ENABLE_MESSAGE_HISTORY=false` (no migration rollback needed;
   data remains but is not served).

## Frontend Build Status

**FRONTEND BUILD BLOCKED — external exact-SHA build required.**

- `node` is available in workspace.
- `npm`/`npx` are NOT in PATH (requires bootstrap per team convention).
- `frontend/node_modules` does not exist.
- Frontend source files (`App.jsx`, `ChatWindow.jsx`, `conversationHistoryLoader.js`)
  are syntactically correct (confirmed via audit).
- Build must be performed in an external environment:
  `cd frontend && npm ci && npm run build`
  Then delete `node_modules/` before deployment.

## Proposed Build Automation (GitHub Actions)

```yaml
name: CI/CD — TransparencE

on:
  push:
    branches: [feature/**, main]
  pull_request:
    branches: [main]

jobs:
  python-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt rapidfuzz pydantic-settings
      - run: |
          pytest tests/ \
            --ignore=tests/test_genie_live_smoke.py \
            --ignore=tests/test_genie_integration_smoke.py \
            --ignore=tests/test_delta_state_live_smoke.py \
            --ignore=tests/test_new_pipeline_live_smoke.py \
            -q --import-mode=importlib -p no:cacheprovider

  frontend-tests-and-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: cd frontend && npm ci
      - run: node tests/test_frontend_conversation_persistence.mjs
      - run: node tests/test_frontend_conversation_reset.mjs
      - run: cd frontend && npm run build
      - name: Verify static assets exist
        run: |
          ls frontend/dist/assets/index-*.js
          ls frontend/dist/assets/index-*.css
      - name: Remove node_modules before artifact
        run: rm -rf frontend/node_modules
      - uses: actions/upload-artifact@v4
        with:
          name: frontend-dist
          path: frontend/dist/

  deployment-gate:
    needs: [python-tests, frontend-tests-and-build]
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    environment: production
    steps:
      - name: Approve before deploy
        run: echo "Deployment approved"
```

## Notes

- The 36 skipped Python tests are `@pytest.mark.asyncio` lifecycle tests that
  require `pytest-asyncio`; they are skipped in this environment but can be
  enabled by adding the plugin.
- The migration has not been applied and must remain unapplied until deployment.
- Static assets have not been modified.
- The Pydantic V2 deprecation warning (PydanticDeprecatedSince20) is pre-existing
  and unrelated to H1 changes.
