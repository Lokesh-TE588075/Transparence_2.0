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
