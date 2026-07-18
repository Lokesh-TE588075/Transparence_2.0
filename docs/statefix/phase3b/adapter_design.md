# Phase 3B Adapter Design

`DurableGenieSessionAdapter` is a standalone service-layer adapter that composes an existing conversation repository bundle with an optional `GenieSessionStore` compatibility cache.

## Purpose

* keep the conversation repository authoritative for conversation existence
* keep the conversation repository authoritative for owner isolation
* keep the conversation repository authoritative for version, status, timestamps, Genie ID, and last message ID
* bridge durable state to the legacy cache without making the cache authoritative
* allow carefully bounded degraded reads from adapter-local confirmed snapshots only

## Public types

* `DurableGenieSessionKey`
* `GenieSessionLookupSource`
* `GenieSessionLookupResult`
* `DurableGenieSessionAdapter`
* translated adapter exceptions for unavailable, conflict, not-found, cache, and closed states

## Construction model

The adapter accepts a repository bundle and an optional cache store. It does not construct repositories, connection providers, pools, SQL clients, or workspace clients. This preserves Phase 3B scope and avoids runtime integration.

## Internal state

The adapter keeps a thread-safe in-memory map of repository-confirmed `ConversationRecord` snapshots keyed by an owner-scoped hashed cache key. Those snapshots are used only for degraded fallback reads after a previous authoritative success.

## Non-goals

* no environment wiring
* no app configuration changes
* no replacement of `GenieSessionStore`
* no change to existing runtime call sites
* no durable-read authority transfer to cache

## Phase 3B Exit Assessment

### Status

Phase 3B: COMPLETE — PASSED

### Exit criteria

* `DurableGenieSessionAdapter` is implemented.
* Repository is authoritative.
* Compatibility cache is non-authoritative.
* Degraded reads require a repository-confirmed snapshot.
* Mutations cannot succeed while the repository is unavailable.
* Optimistic-concurrency conflicts are surfaced.
* Owner isolation is maintained.
* Adapter lifecycle closes the injected repository bundle.
* `GenieSessionStore` was not modified.
* No runtime integration occurred.
* No live Lakebase connection occurred.
* No credential was generated.
* No pool was opened.
* No SQL was executed.
* No deployment or app restart occurred.

### Test evidence

* focused adapter tests: 75 passed
* combined persistence tests: 367 passed
* complete non-live tests: 1278 passed
* zero failures
* zero final collection errors
* exactly four approved live suites excluded

### Remaining risks

* the adapter is not yet wired into runtime
* adapter-local degraded snapshots are process-local and non-authoritative
* actual Lakebase authentication and repository operations still require
  later controlled live validation
* runtime rollout must remain disabled by default

### Phase 3C gate

Phase 3C runtime-factory wiring behind disabled-by-default feature flags is
safe to begin.

Phase 3C must not yet replace `GenieSessionStore` behaviour for normal
production traffic or deploy the changes.
