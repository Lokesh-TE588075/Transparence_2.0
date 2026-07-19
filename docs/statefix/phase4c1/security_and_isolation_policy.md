# Phase 4C1: Security and Isolation Policy

## Owner-Key Isolation

- Each request derives its own `owner_key` independently.
- No process-global owner key exists.
- No cross-request leakage is possible.
- Concurrent requests remain isolated.
- The pipeline does not store `owner_key` on its instance.
- No owner-key cache exists.

## Excluded Operations

Phase 4C1 does NOT:
- Access the durable adapter.
- Access the conversation repository.
- Open any Lakebase connection.
- Generate any credential.
- Open any connection pool.
- Execute any SQL.
- Store the key in session state.
- Use the key as a session key.
- Log the key.
- Include the key in responses.

## Error Policy

A malformed `owner_key` indicates an internal contract violation.
The error:
- Fails before any Genie interaction.
- Uses a static sanitized message.
- Never exposes the supplied value.
- Never logs the value.
- Is caught by a dedicated `_OwnerKeyContractError` handler.
- Results in `status="error"` and `fallback_recommended=False`.
- Custom pipeline fallback is PROHIBITED for this failure.
- No request is processed without the trusted owner key.

Normal unrelated Genie failures (timeouts, client errors, execution
errors) continue to set `fallback_recommended=True` and allow the
custom pipeline fallback to execute.

## Audit Principal Exclusion

`audit_principal` is explicitly excluded from pipeline plumbing.
It remains attached to `request.state` only and is not passed to
the pipeline in any form.

## No Persistent Dependency Stubs

All Phase 4C1 tests use the real `rapidfuzz` dependency.
No `sys.modules` substitutions remain in test files.
Test collection order does not affect results.
