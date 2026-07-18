# Ownership and Permissions

Phase: 2B2B2
Date: 2026-07-18

## Planned model (from phase specification)

The spec called for:
```sql
ALTER TABLE transparence_state.app_conversation OWNER TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
ALTER SCHEMA transparence_state OWNER TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
ALTER ROLE "488a0acb-5804-42f0-98b1-a02cc13c4573" IN DATABASE databricks_postgres SET search_path = transparence_state, public;
```

## Actual model (what was executed)

The above three statements are all blocked by Databricks Lakebase platform restrictions:

| Statement | Blocker |
|---|---|
| `ALTER TABLE ... OWNER TO "SP"` | Requires `SET ROLE "SP"` ability; `GRANT "SP" TO current_user` requires ADMIN OPTION (not present on SP role) |
| `ALTER SCHEMA ... OWNER TO "SP"` | Same -- requires SET ROLE ability |
| `ALTER ROLE "SP" ... SET search_path` | Requires CREATEROLE attribute + ADMIN OPTION on the SP role; `databricks_superuser` has neither for the SP |

### Investigation summary

| Role | superuser | createrole | set_option on SP |
|---|---|---|---|
| `lokesh.choraria@te.com` | False | True | No |
| `databricks_superuser` | False (pseudo) | -- | No |
| `cloud_admin` | True | -- | No members |
| `488a0acb-5804-42f0-98b1-a02cc13c4573` | False | False | n/a |

All known paths to transfer ownership or alter the SP role were confirmed blocked.
SP authentication is prohibited by phase constraints and would not be applicable
for DDL executed by the project owner.

## Adopted model

Schema and table remain owned by `lokesh.choraria@te.com`. The SP receives minimum operational
privileges via GRANT:

```sql
GRANT USAGE ON SCHEMA transparence_state TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE transparence_state.app_conversation TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
```

PUBLIC has no CREATE or USAGE on `transparence_state` (explicitly revoked).

## search_path requirement

`lakebase_conversation_repository.py` uses `_TABLE_NAME = "app_conversation"` with
no schema qualification. This requires `transparence_state` to be in the SP's
search_path when the app connects.

`ALTER ROLE ... SET search_path` was blocked (see above).

**Required action for Phase 2C**: The Lakebase connection provider MUST set
search_path at connection time:

```python
conn = psycopg.connect(
    host=...,
    dbname="databricks_postgres",
    user=sp_username,
    password=...,
    sslmode="require",
    options="-c search_path=transparence_state,public"
)
```

This is a Phase 2C implementation requirement. It is documented in the migration
file header comment (section "NOTE: search_path is NOT set as a role attribute").

## Revision path

If a future platform release allows ownership transfer or ALTER ROLE for app SP
roles, the following migration can be applied to bring the state in line with the
original spec:

```sql
-- Future: run as superuser or once SET ROLE "SP" is permitted
ALTER TABLE transparence_state.app_conversation OWNER TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
ALTER SCHEMA transparence_state OWNER TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
ALTER ROLE "488a0acb-5804-42f0-98b1-a02cc13c4573" IN DATABASE databricks_postgres SET search_path = transparence_state, public;
-- Then remove the GRANT statements (SP as owner has implicit privileges)
REVOKE USAGE ON SCHEMA transparence_state FROM "488a0acb-5804-42f0-98b1-a02cc13c4573";
REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE transparence_state.app_conversation FROM "488a0acb-5804-42f0-98b1-a02cc13c4573";
```
