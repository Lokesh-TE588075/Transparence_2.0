# Conversation Reset Coordinator Contract

## Phase 4C4B1 — Durable Reset Coordinator

### Purpose

Orchestrates authoritative durable reset of a Genie conversation session,
ensuring process-local session removal occurs only after confirmed durable
state transition.

### Input Contract

| Parameter | Type | Validation |
|-----------|------|-----------|
| `owner_user_id_hash` | str | Exactly 64 lowercase hexadecimal characters |
| `frontend_conversation_id` | str | DurableGenieSessionKey validation (non-empty, no `@`) |

The coordinator does NOT:
- Parse request headers
- Accept email addresses
- Derive owner identity
- Accept Genie conversation ID, message ID, record ID, status, or version

### Dependencies (Injected)

- `DurableGenieSessionAdapter` — durable state operations
- `GenieSessionStore` — process-local session removal

### Authoritative-State Requirement

Reset is a state-changing operation. Degraded, unconfirmed, or cache-only
adapter results are NEVER accepted as durable reset success. When the
adapter returns a degraded result, the coordinator fails closed with
`ResetCoordinatorUnavailableError`.

### Result Contract

`ResetResult` exposes only:
- `success: bool`
- `outcome: ResetOutcome` (RESET | ALREADY_INACTIVE | TOMBSTONE_CREATED)

No raw owner hash, frontend ID, record ID, Genie ID, or version is exposed.

### Exception Hierarchy

| Exception | Meaning |
|-----------|---------|
| `ResetCoordinatorInvalidInputError` | Input validation failed |
| `ResetCoordinatorConflictError` | Version conflict, not resolvable idempotently |
| `ResetCoordinatorUnavailableError` | Durable backend unavailable |
| `ResetCoordinatorInternalError` | Unexpected internal failure |

All exception `repr()` values are sanitized — no identifiers exposed.

### Status Outcomes

| Initial Status | Action | Outcome |
|---------------|--------|---------|
| ACTIVE | CAS → RESET | `RESET` |
| RESET | No mutation | `ALREADY_INACTIVE` |
| STALE | No mutation | `ALREADY_INACTIVE` |
| EXPIRED | No mutation | `ALREADY_INACTIVE` |
| Missing | get_or_create → CAS → RESET | `TOMBSTONE_CREATED` |

### Prohibited Operations

- No `delete`
- No `bind_genie_conversation`
- No `update_last_genie_message`
- No `touch`
- No direct repository access
