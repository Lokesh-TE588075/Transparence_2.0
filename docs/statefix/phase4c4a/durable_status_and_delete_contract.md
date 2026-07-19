# Phase 4C4A — Durable Status and Delete Contract

## Document Purpose

Complete verified contract for `set_status` and `delete_conversation` as
implemented in `conversation_repository.py` (Protocol + InMemoryConversationRepository)
and surfaced through `durable_genie_session_adapter.py`.

---

## 1. Status Enum

**File:** `app/services/conversation_repository.py`, line 80  
**Class:** `ConversationStatus(str, Enum)`

| Value | Meaning |
|-------|---------|
| `ACTIVE` | Conversation is in normal use |
| `STALE` | Exceeded inactivity threshold; may be refreshed or reset |
| `RESET` | Explicitly reset by user or system |
| `EXPIRED` | Passed hard expiry TTL; should not be resumed |

**Transition validation:** None.  The repository does **not** enforce
allowed transitions.  Any status can be set to any other status via
`set_status`, including `RESET → ACTIVE`.  Transition governance is the
caller’s responsibility.

---

## 2. set_status — Repository Contract

**File:** `conversation_repository.py`, line 389 (Protocol), line 760 (InMemory impl)

### 2.1 Signature

```python
def set_status(
    self,
    owner_user_id_hash: str,
    conversation_id: str,
    status: ConversationStatus,
    *,
    expected_version: int,
    now: Optional[datetime] = None,
) -> ConversationRecord:
```

### 2.2 Semantics

| Aspect | Behaviour |
|--------|-----------|
| **Input key fields** | `owner_user_id_hash` + `conversation_id` (internal UUID) |
| **Target status** | Any `ConversationStatus` value |
| **expected_version** | Required; must equal the record’s current `version` |
| **Compare-and-swap** | Atomic: reads stored version, compares with expected, applies if match |
| **Returned record** | New frozen `ConversationRecord` with updated status, incremented version, updated `updated_at` |
| **Version increment** | Exactly +1 on success |
| **updated_at** | Set to `now` (or current UTC if `now` is None) |
| **last_active_at** | **NOT updated** — `set_status` does not touch last_active_at |
| **Same-status behaviour** | Setting the same status as current **still increments version** and updates `updated_at`.  It is NOT a no-op. |
| **Not-found** | Raises `ConversationNotFoundError` (ownership silently enforced) |
| **Version-conflict** | Raises `ConversationVersionConflictError`; record unchanged |
| **Repository-unavailable** | Raises `ConversationRepositoryUnavailableError` (Lakebase impl only) |
| **Allowed transitions** | ALL transitions technically permitted (no state machine) |
| **RESET → ACTIVE** | Technically permitted by the repository; no guard exists |
| **Genie IDs** | Remain unchanged; `set_status` modifies only `status`, `version`, `updated_at` |
| **Raw identifiers in exceptions** | InMemory impl includes `conversation_id` in error messages.  The adapter translates all exceptions to sanitized static messages (line 521–536). |

### 2.3 InMemory Implementation (line 760–784)

```python
def set_status(self, owner_user_id_hash, conversation_id, status, *, expected_version, now=None):
    with self._lock:
        record = self._get_owned(owner_user_id_hash, conversation_id)
        if record is None:
            raise ConversationNotFoundError(...)
        _check_version(record, expected_version)
        ts = _validate_now(now)
        updated = self._evolve(
            record,
            status=status,
            version=record.version + 1,
            updated_at=ts,
        )
        self._store(updated)
        return updated
```

---

## 3. set_status — Adapter Contract

**File:** `durable_genie_session_adapter.py`, line 364

### 3.1 Signature

```python
def set_status(
    self,
    key: DurableGenieSessionKey,
    status: ConversationStatus,
    *,
    expected_version: int,
    now: Optional[datetime] = None,
) -> ConversationRecord:
```

### 3.2 Adapter Behaviour

1. Calls `_require_existing_record(key)` → looks up record by `(owner_hash, frontend_id)` via `get_by_frontend_id`.
2. On miss: raises `DurableGenieSessionNotFoundError` (sanitized).
3. On unavailable: raises `DurableGenieSessionUnavailableError` (sanitized).
4. Calls `self._repository.set_status(owner, conversation_id, status, expected_version=..., now=...)`.
5. On success: updates confirmed snapshot cache; refreshes compatibility cache.
6. On `ConversationVersionConflictError`: translates to `DurableGenieSessionVersionConflictError`.
7. On `ConversationNotFoundError`: translates to `DurableGenieSessionNotFoundError`.
8. On generic `ConversationRepositoryError`: translates to `DurableGenieSessionAdapterError`.

### 3.3 Confirmed Snapshot Update

After successful `set_status`, the adapter calls `_store_confirmed_snapshot(key, record)` — the local snapshot reflects the new status and version.

---

## 4. delete_conversation — Repository Contract

**File:** `conversation_repository.py`, line 478 (Protocol), line 843 (InMemory impl)

### 4.1 Signature

```python
def delete_conversation(
    self,
    owner_user_id_hash: str,
    conversation_id: str,
) -> bool:
```

### 4.2 Semantics

| Aspect | Behaviour |
|--------|-----------|
| **Deletion type** | Physical removal from all indexes |
| **expected_version** | **NOT required** — unconditional delete |
| **Optimistic concurrency** | **NOT protected** — no CAS; deletes regardless of current version |
| **Owner isolation** | Record must match `owner_user_id_hash`; different owner treated as not-found |
| **Missing record** | Returns `False` (no exception; no ownership leakage) |
| **Return type** | `bool` — True if deleted, False if not found |
| **Same-key recreation after delete** | Permitted; `create_conversation` with same logical key creates a new record at version 1 |
| **Version of recreated record** | Always 1 (fresh) |
| **Audit history** | Completely lost; no soft-delete or tombstone |
| **Race with concurrent update** | **Unprotected**: a concurrent `set_status` or `bind_genie_conversation` that succeeds between the lookup and the physical remove has its mutation silently destroyed |

### 4.3 InMemory Implementation (line 843–853)

```python
def delete_conversation(self, owner_user_id_hash, conversation_id):
    with self._lock:
        record = self._get_owned(owner_user_id_hash, conversation_id)
        if record is None:
            return False
        self._remove(record)
        return True
```

---

## 5. delete — Adapter Contract

**File:** `durable_genie_session_adapter.py`, line 388

### 5.1 Signature

```python
def delete(self, key: DurableGenieSessionKey) -> bool:
```

### 5.2 Adapter Behaviour

1. Calls `_ensure_open()`.
2. Looks up current record via `get_by_frontend_id`.
3. If record is `None`: removes confirmed snapshot, clears compatibility cache, returns `False`.
4. Calls `repository.delete_conversation(owner, conversation_id)`.
5. On success (`True`): removes confirmed snapshot; calls `_clear_compatibility_cache_after_delete` which invokes `reset_session` on the cache store.
6. On `False` (race): still removes snapshot and clears cache, returns `False`.
7. On `unavailable`: raises `DurableGenieSessionUnavailableError`.

### 5.3 Cache Cleanup

`_clear_compatibility_cache_after_delete` calls `cache_store.reset_session(cache_key)` which sets `is_active=False`, clears Genie IDs and message IDs.

---

## 6. Inactive-Record Recovery Behaviour

### 6.1 GenieSessionStore (In-Memory)

**Method:** `_get_or_create_session` (line 135)  
**Condition:** `session is None or session.is_expired(now) or not session.is_active`

When this condition is true, a **new fresh session** is created, replacing
the old one.  This means:

- Expired sessions → treated as miss, new session created.
- Inactive sessions (`is_active=False`) → treated as miss, new session created.

**`get_session`** (line 285) returns `None` for expired or inactive sessions.

### 6.2 Durable Adapter

**`adapter.load(key)`** (line 250):
- Calls `repository.get_by_frontend_id`.
- Returns the record **regardless of status** (ACTIVE, STALE, RESET, EXPIRED).
- The adapter does NOT filter by status; it returns whatever the repository holds.

**`adapter.get_or_create(key)`** (line 215):
- Calls `repository.create_conversation`.
- `create_conversation` is **idempotent on logical key** — if a record exists (ANY status), it returns it unchanged.
- This means calling `get_or_create` on a RESET key returns the RESET record.

### 6.3 Durable vs In-Memory Status: Separate Systems

**A. Durable lifecycle (`ConversationRecord.status`):**

`ACTIVE`, `STALE`, `RESET`, `EXPIRED` — persisted in the repository.  The
durable adapter `load()` method returns records of ANY status.  It does NOT
filter.  Status evaluation is the pipeline’s responsibility.

**B. In-memory lifecycle (`GenieSession.is_active` + expiry):**

`GenieSessionStore` governs only the process-local session object.  Its
`is_active` flag and `expires_at` field are separate from durable
`ConversationStatus`.  They operate on independent timelines and
independent data stores.

`GenieSessionStore` alone does NOT prevent durable recovery.  It prevents
only in-memory session reuse within a single container lifetime.

### 6.3.1 Current Pipeline Status Classification (Phase 4C2A/4C3)

**Method:** `GeniePipeline._durable_session_lookup()` (genie_pipeline.py,
line 1390)

**Enum:** `_DurableLookupOutcome` (line 213) with values: `DISABLED`,
`MISS`, `RECOVERED`.

GeniePipeline accesses the durable adapter through the request-scoped
runtime bundle (`self._durable_session_runtime_bundle`).  The method
`_durable_session_lookup` calls `adapter.load(durable_key)` and classifies
the result:

**Current classification logic (line 1477–1478):**
```python
if record.status != ConversationStatus.ACTIVE:
    return _DurableRequestContext(
        outcome=_DurableLookupOutcome.MISS, key=durable_key, record=None
    )
```

| Durable status | Current outcome | Consequence |
|----------------|-----------------|-------------|
| No record | MISS | New Genie conversation started; `get_or_create` called in writeback |
| ACTIVE + bound | RECOVERED | Existing Genie conversation resumed via `send_message` |
| ACTIVE + unbound | MISS | New Genie conversation started |
| **RESET** | **MISS** | **UNSAFE: enters new-conversation flow** |
| **STALE** | **MISS** | **UNSAFE: enters new-conversation flow** |
| **EXPIRED** | **MISS** | **UNSAFE: enters new-conversation flow** |

### 6.3.2 Why Inactive-as-MISS Is Unsafe

When a non-ACTIVE record is classified as MISS:

1. `_run_inner()` executes — a new Genie conversation is started externally.
2. `_maybe_persist_durable_writeback()` is called after execution.
3. Writeback calls `adapter.get_or_create(key)` which invokes
   `repository.create_conversation(owner, frontend_id)`.
4. `create_conversation` is idempotent on logical key — it **returns the
   existing RESET record unchanged** (status remains RESET, version unchanged).
5. The writeback then attempts `bind_genie_conversation` on the returned
   record, potentially mutating a record that should be permanently inactive.

This means an inactive (RESET/STALE/EXPIRED) durable record does NOT
currently block Genie execution.  It only prevents session **recovery**.
The pipeline still creates an external Genie conversation and may attempt
to mutate the inactive durable record.

### 6.3.3 Required Phase 4C4B Fix: Dedicated INACTIVE Outcome

Phase 4C4B must introduce a dedicated outcome:

```python
class _DurableLookupOutcome:
    DISABLED  = "DISABLED"
    MISS      = "MISS"
    RECOVERED = "RECOVERED"
    INACTIVE  = "INACTIVE"   # NEW: blocks execution entirely
```

When the durable lookup finds a record with status in
`{RESET, STALE, EXPIRED}`:

- Return `_DurableRequestContext(outcome=_DurableLookupOutcome.INACTIVE, ...)`
- `_run_inner()` must NOT execute.
- `start_conversation()` must NOT execute.
- `send_message()` must NOT execute.
- `get_or_create` must NOT execute.
- `bind_genie_conversation` must NOT execute.
- `update_last_genie_message` must NOT execute.
- Custom fallback must NOT execute.
- Return a static sanitized response immediately.
- `fallback_recommended=False`
- No raw identifiers exposed (owner, frontend ID, record ID, Genie IDs,
  status value, version).

**Recommended static response:**
```python
{
    "status": "inactive",
    "message": "This conversation is no longer active. Start a new chat.",
    "is_table": False,
    "fallback_recommended": False,
}
```

### 6.3.4 Post-Writeback Status Validation (Boundary 2)

The INACTIVE outcome at initial lookup (Boundary 1) cannot prevent the
reset-versus-MISS race because the initial lookup legitimately returns MISS
when no record exists yet.

Phase 4C4B must add a second status check inside
`_persist_new_durable_conversation` (line 1760 of `genie_pipeline.py`):

**Current code (unsafe):**
```python
record = lookup_result.record
# Proceeds directly to inspect genie_conversation_id binding
# NO status check
```

**Required Phase 4C4B fix:**
```python
record = lookup_result.record
if record.status != ConversationStatus.ACTIVE:
    # Tombstone or inactive record — abort writeback
    raise _DurableWritebackError(
        "Durable record is inactive (status={}).".format(record.status.value)
    )
# Only proceed with bind when status is confirmed ACTIVE
```

This closes the TOCTOU window: even when initial lookup returned MISS and
`_run_inner()` started a Genie conversation, a concurrent reset tombstone
blocks the bind operation.

**Proven race sequence:**
1. Chat request → `_durable_session_lookup` → MISS (no record).
2. `_run_inner()` → Genie conversation started externally.
3. Concurrent reset → `get_or_create` → creates RESET tombstone.
4. Chat writeback → `get_or_create` → returns RESET record unchanged.
5. Status check → RESET → raises `_DurableWritebackError`.
6. Bind never called; old ID remains inactive.

Without Boundary 2, step 4 would proceed to `bind_genie_conversation` and
the old ID would become fully bound and recoverable — violating the reset
contract.

### 6.4 Critical Finding: Same-Key get_or_create on RESET Record

For a record with:
- Same `owner_user_id_hash`
- Same `frontend_conversation_id`
- Status `RESET`

`adapter.get_or_create(key)` calls `repository.create_conversation(owner, frontend_id)`.  
Because `create_conversation` is **idempotent on logical key**:

- The existing RESET record is **returned unchanged**.
- It is **NOT** automatically reactivated.
- Its existing Genie conversation ID remains present (if previously bound).
- Its existing last Genie message ID remains present.
- `bind_genie_conversation` **CAN** run on it (no status validation).
- `update_last_genie_message` **CAN** run on it (no status validation).
- Status is **NOT** validated by those operations.

**A fresh ACTIVE row CANNOT be inserted with the same logical key.**  
The unique constraint `(owner_user_id_hash, frontend_conversation_id)` blocks it.

### 6.5 Resulting Rule

- A **new frontend conversation ID is MANDATORY** for New Chat.
- Same-key reuse with a RESET record would accidentally return/mutate the
  reset record.
- Explicit reactivation (`RESET → ACTIVE`) would require a separate future
  contract with deliberate version-checked `set_status`.

### 6.6 Degraded Read Behaviour

When repository is unavailable, `adapter.load` and `adapter.get_or_create`
fall back to a confirmed snapshot (if one was previously stored).  A degraded
snapshot of an inactive (RESET/STALE/EXPIRED) record IS returned to the
caller with `degraded=True`.  The pipeline must still check status and reject
it for Genie communication.

---

## 7. Test Coverage

| Behaviour | Test File | Test Class/Method |
|-----------|-----------|-------------------|
| set_status version increment | `test_conversation_repository.py` | version CAS tests |
| set_status not-found | `test_conversation_repository.py` | ownership tests |
| set_status same-status increments version | `test_conversation_repository.py` | status transition tests |
| delete returns False on miss | `test_conversation_repository.py` | delete tests |
| delete is physical | `test_conversation_repository.py` | recreation after delete |
| Adapter set_status translates exceptions | `test_durable_genie_session_adapter.py` | version conflict tests |
| Adapter delete clears snapshot | `test_durable_genie_session_adapter.py` | delete tests |
| create_conversation idempotent on key | `test_conversation_repository.py` | idempotent create tests |
| get_or_create returns RESET record unchanged | (implicit from idempotent create contract) |

---

*Phase 4C4A — inspection only.  No code modified.*
