# Backend Selection

Phase 3A introduces explicit backend selection through environment parsing.

## Environment variables

* `CONVERSATION_REPOSITORY_BACKEND`
* `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY`

## Defaults

* `CONVERSATION_REPOSITORY_BACKEND=memory`
* `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=false`

Lakebase is therefore disabled by default.

## Accepted backend values

Backend parsing is case-insensitive and whitespace-safe.

Supported values:

* `memory`
* `lakebase`

Unknown values fail with a sanitized configuration error.

## Accepted Boolean values

Truthy values:

* `true`
* `1`
* `yes`
* `on`

False values:

* `false`
* `0`
* `no`
* `off`
* empty string

Unknown Boolean values fail safely with a sanitized configuration error.

## Selection policy

* `backend=memory` always returns the in-memory repository.
* `backend=memory` with the enable flag set to true is allowed and still returns memory.
* `backend=lakebase` requires `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=true`.
* `backend=lakebase` with the enable flag false is rejected.
* there is no silent fallback from explicit Lakebase selection to memory.

## Import safety

No environment variables are read at module import time. Environment access occurs only when `ConversationRepositoryFactorySettings.from_environment()` is called.
