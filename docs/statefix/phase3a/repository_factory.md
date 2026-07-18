# Repository Factory

Phase 3A adds `app/services/conversation_repository_factory.py` as a construction boundary for conversation-state repositories.

## Scope

The factory can create either:

* an `InMemoryConversationRepository`
* a durable Lakebase-backed repository bundle

Phase 3A intentionally stops at construction. The factory is not imported by runtime application modules, is not wired into `GenieSessionStore`, chat routes, app startup, or any active request path.

## Public components

* `ConversationRepositoryBackend`
  * `MEMORY`
  * `LAKEBASE`
* `ConversationRepositoryFactorySettings`
* `ConversationRepositoryBundle`
* `ConversationRepositoryFactory`
* `create_conversation_repository(environ=None)`

## Construction behavior

### Memory path

When backend selection resolves to `MEMORY`, the factory:

* returns a fresh `InMemoryConversationRepository`
* marks bundle `backend=MEMORY`
* marks bundle `durable=False`
* does not create durable-backend settings
* does not create a connection provider
* does not create a pool
* does not create a credential
* does not execute SQL

### Durable path

When backend selection resolves to `LAKEBASE` and the enable flag is true, the factory:

* loads durable-backend settings lazily
* constructs a connection-provider owner lazily
* constructs the durable repository lazily
* returns bundle `backend=LAKEBASE`
* marks bundle `durable=True`

Construction alone does not open a physical connection. The durable provider remains lazy and defers pool opening, credential minting, and SQL execution until an actual connection request occurs.

## Dependency injection

The factory constructor supports injection of:

* environment mapping
* memory repository factory
* durable settings factory
* durable connection-owner factory
* durable repository factory

This keeps Phase 3A testable with fakes only and avoids any need to touch live infrastructure.
