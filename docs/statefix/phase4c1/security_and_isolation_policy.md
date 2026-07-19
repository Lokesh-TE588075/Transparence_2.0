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
- Is caught by the pipeline's top-level error handler.
- Results in `status="error"` and `fallback_recommended=True`.

## Audit Principal Exclusion

`audit_principal` is explicitly excluded from pipeline plumbing.
It remains attached to `request.state` only and is not passed to
the pipeline in any form.
