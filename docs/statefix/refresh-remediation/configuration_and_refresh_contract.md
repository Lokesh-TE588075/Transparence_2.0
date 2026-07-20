# Refresh Remediation Configuration and Refresh Contract

## Scope

Controlled remediation was limited to the deployed refresh/durable-conversation contract.

Production-source files changed:

* `frontend/src/App.jsx`
* `app.yaml`
* `app/config.py`

No production edits were made to backend business logic, Genie prompts, Lakebase schema, migrations, dependencies, static build assets, deployment resources, assistant instructions, or `uv`.

## Deployed app.yaml profile

The deployed profile is intentionally the controlled architecture, not the older smoke profile.

Required effective values:

* `USE_GENIE_BACKEND=true`
* `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true`
* `ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true`
* `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=true`
* `CONVERSATION_REPOSITORY_BACKEND=lakebase`
* `GENIE_FALLBACK_TO_CUSTOM_PIPELINE=false`
* `NEW_PIPELINE_FALLBACK_TO_OLD=false`
* `GENIE_DEBUG=false`
* `NEW_PIPELINE_DEBUG=false`
* `CONVERSATION_STATE_CLEANUP_HARD_DELETE=false`
* `GENIE_EXPORT_MODE=returned_rows_only`

Preserved bindings and identifiers:

* `GENIE_SPACE_ID` preserved
* `DATABRICKS_SQL_WAREHOUSE_PATH` preserved
* `SHIPMENT_TABLE_NAME` preserved
* `LAKEBASE_ENDPOINT_NAME` remains `valueFrom: postgres`
* `CONVERSATION_OWNER_HMAC_SECRET` remains `valueFrom: conversation-owner-hmac-secret`

Static verification confirmed exactly one declaration for each controlled flag and no duplicate environment-variable names in `app.yaml`.

## Coordinated deployment assertions

Deployment-contract tests were aligned to the controlled profile instead of the retired disabled profile.

Static assertions now prove:

* trusted identity is enabled
* durable adapter is enabled
* Lakebase repository selection is enabled and paired with `lakebase`
* legacy Genie fallback is disabled
* legacy accuracy fallback is disabled
* debug remains disabled
* hard deletion remains disabled
* secret/resource bindings remain indirect via `valueFrom`

Runtime-path tests that intentionally exercise disabled combinations through mocked environment variables were preserved.

## config.py corrections

Confirmed source corrections:

* `"*"` was removed from `CORS_ALLOWED_ORIGINS`
* approved localhost origins remain: `http://localhost:5173`, `http://localhost:3000`, `http://localhost:8000`
* `CONVERSATION_STATE_CLEANUP_HARD_DELETE` is declared exactly once
* no unrelated configuration default was changed in this remediation

## main.py guardrails

No scheduler was added.
No cleanup background job was registered.
The reset route inclusion remains present, but there is no new periodic cleanup registration in `main.py`.
