# Phase 2C Runtime Configuration

Phase: Lakebase OAuth connection provider
Date: 2026-07-18
Status: COMPLETE

## Files changed

* `requirements.txt`
* `app.yaml`
* `app/services/lakebase_connection_provider.py`
* `tests/test_lakebase_connection_provider.py`

No runtime module or test file outside this approved set was edited.

## Requirements changes

`requirements.txt` now contains the minimum dependencies needed for the provider:

* `databricks-sdk>=0.118.0`
* `psycopg[binary,pool]>=3.1.0`

Rationale:

* `databricks-sdk` provides `WorkspaceClient().postgres.generate_database_credential(...)`.
* `psycopg[binary]` provides psycopg3 without requiring separate system libpq installation.
* `psycopg[pool]` provides `psycopg_pool.ConnectionPool`.

## App configuration changes

`app.yaml` now includes:

* `LAKEBASE_ENDPOINT_NAME`
  * source: `valueFrom: postgres`

This keeps the endpoint resource name injected by Databricks Apps at runtime instead of hardcoding a `projects/.../branches/.../endpoints/...` path into source control.

## Environment variables consumed by Phase 2C

Required at runtime:

* `PGHOST`
* `PGDATABASE`
* `PGPORT`
* `PGUSER`
* `PGSSLMODE`
* `LAKEBASE_ENDPOINT_NAME`

Optional:

* `PGAPPNAME`

## Enforced runtime behavior

The provider always sets PostgreSQL options:

* `-c search_path=transparence_state,public`

This is the required workaround documented in Phase 2B2B2 because platform constraints prevented setting the app service-principal role search path permanently in Lakebase.

## Non-goals for Phase 2C

Phase 2C does not:

* change repository SQL behavior
* introduce live database tests
* alter schema ownership or role configuration
* enable session persistence in production code yet

It only adds the safe connection provider and its isolated unit coverage.
