# Phase 2C Provider and Test Inventory

Phase: Lakebase OAuth connection provider
Date: 2026-07-18
Status: COMPLETE

## Provider module inventory

File: `app/services/lakebase_connection_provider.py`

Primary exported objects:

* `LakebaseConnectionSettings`
* `LakebaseConnectionProviderError`
* `LakebaseConfigurationError`
* `LakebaseCredentialError`
* `LakebasePoolUnavailableError`
* `LakebasePoolClosedError`
* `LakebaseConnectionProvider`

Internal helper used by tests:

* `_build_oauth_connection_class(...)`

## Test module inventory

File: `tests/test_lakebase_connection_provider.py`

The Phase 2C test suite is fake-based only and covers these areas:

* settings validation
* lazy import / lazy construction behavior
* OAuth credential request timing
* endpoint propagation to the SDK credential call
* per-physical-connection fresh credential behavior
* psycopg_pool configuration values
* forced search_path behavior
* provider lifecycle (`__call__`, `close`, context-manager close)
* thread-safe single pool creation on concurrent first access
* sanitized error behavior
* repository compatibility contract
* AST parse validity for provider and test module
* import guardrails (`psycopg3` and `psycopg_pool`, not `psycopg2` or SQLAlchemy)

## Test doubles used

The suite uses only controlled fakes/mocks:

* `FakeCredential`
* `FakePostgresAPI`
* `FakeWorkspaceClient`
* `FakeBaseConnection`
* `FakePooledConnection`
* `FakeConnectionContext`
* `FakePool`
* `CapturingPoolFactory`
* `NoCheckPoolFactory`
* `ExplodingEnviron`

These doubles ensure the tests never:

* open sockets
* connect to Lakebase
* execute SQL
* depend on a running Databricks App container

## Repository compatibility checkpoint

The provider was explicitly exercised against `LakebaseConversationRepository` construction and connection-provider expectations. Phase 2C kept repository integration additive and non-invasive.
