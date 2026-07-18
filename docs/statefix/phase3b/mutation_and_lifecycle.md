# Mutation and Lifecycle Semantics

## Supported operations

* `get_or_create()`
* `load()`
* `bind_genie_conversation()`
* `update_last_genie_message()`
* `touch()`
* `set_status()`
* `delete()`
* `close()`

## Mutation semantics

All write-style operations first resolve the durable record from the repository, then perform the repository mutation, then refresh the adapter-local snapshot, and only then attempt compatibility-cache maintenance.

This preserves the required ordering:

* repository mutation first
* cache synchronization second
* cache failure never rolls back durable success

## Close semantics

The adapter owns only the provided repository bundle lifecycle.

* `close()` closes the bundle once
* repeated `close()` calls are safe
* `with DurableGenieSessionAdapter(...)` is supported
* post-close API calls raise `DurableGenieSessionClosedError`

## Concurrency notes

An internal `threading.RLock` protects adapter closed-state and confirmed-snapshot updates. The lock does not replace repository-level optimistic concurrency; durable write conflicts still surface via translated version-conflict exceptions.

## Sanitization policy

Public adapter errors do not expose:

* owner hashes
* Genie conversation IDs
* message IDs
* SQL text
* database hostnames
* endpoint resource paths
