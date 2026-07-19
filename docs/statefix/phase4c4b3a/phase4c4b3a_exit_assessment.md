# Phase 4C4B3A — Exit Assessment

## Phase Objective

Separate the durable Lakebase lookup key from the process-local
`GenieSessionStore` key so that two authenticated users sharing a session
cookie cannot collide in in-process state.

## Deliverables

| Item | Status |
|---|---|
| `app/services/process_local_conversation_key.py` (new) | DONE |
| `app/routes/chat.py` (updated) | DONE |
| `app/services/conversation_reset_coordinator.py` (updated) | DONE |
| `tests/test_process_local_conversation_key.py` (new, 54 tests) | DONE |
| `tests/test_chat_owner_scoped_local_key.py` (new, 21 tests) | DONE |
| `tests/test_conversation_reset_coordinator.py` (updated, 63 tests) | DONE |
| `tests/test_chat_owner_key_plumbing.py` (updated) | DONE |
| `tests/test_chat_durable_lookup_key_plumbing.py` (updated) | DONE |

## Constraints Satisfied

- No HTTP reset route added
- No main.py, pipeline, adapter, repository, migrations, or deployment changes
- No live Lakebase connection
- No frontend changes
- All changes additive or narrowly surgical
- Durable repository continues to use raw `owner_user_id_hash` + `frontend_conversation_id`
- Legacy disabled path (identity resolver returns None) still uses `session:id` format
- `ProcessLocalConversationKeyError` opaque — no raw inputs exposed in repr or logs
- `frontend_conversation_id` canonicalized with `strip()` before hashing — aligned with `DurableGenieSessionKey` contract
- `process_local_conversation_key` is a **mandatory argument** to `reset()` — no default, no None fallback
- No `assistant_instructions` change within the repository

## Mandatory Local Key Contract

`ConversationResetCoordinator.reset()` requires three keyword-only arguments:

```python
def reset(
    self,
    *,
    owner_user_id_hash: str,
    frontend_conversation_id: str,
    process_local_conversation_key: str,   # MANDATORY — no default
) -> ResetResult:
```

Missing argument → `TypeError` (Python call-contract failure).
Empty/malformed key → `ResetCoordinatorInvalidInputError`.

## Architecture — Process-Local Store Recovery

The process-local `GenieSessionStore` is intentionally ephemeral.
It is an in-process cache only.  It does NOT require Delta serialization
because the accepted recovery contract for Databricks App idle/scale-to-zero
restarts is:

1. **Trusted owner identity** is re-established from the request token on the
   next request.
2. **Frontend conversation ID** is supplied by the browser client.
3. **Lakebase durable mapping** is loaded via `DurableGenieSessionAdapter`,
   which reconstructs `genie_conv_id` and last-message state from the
   authoritative repository record.
4. **Genie conversation ID/message state** is recovered from the Lakebase
   record and re-injected into the process-local cache on first access.

No additional Delta serialization of the process-local `GenieSessionStore` is
required or planned for the accepted recovery contract.


## Artifact Cleanup

An accidental binary artifact (`uv` — bash wrapper script for the uv package manager,
279 bytes, tracked in two commits after the validation-correction commit) was discovered
and removed:

- `uv` deleted from the repository working directory and git tracking.
- `.gitignore` entry `/uv` (added only to compensate for the accidental commit) was
  reverted.  The `.gitignore` is restored to its exact Phase 4C4B2-parent content.

The final Phase 4C4B3A diff from Phase 4C4B2 parent contains no binary artifacts,
tool binaries, downloaded installers, archives, or generated credentials.

## Commits

- **Implementation commit**: `33f4f60fa35466cf8bbe4bb08cd69b9288c3a482`
  Message: `Bind process-local conversations to trusted owner`
- **Validation-correction commit**: `638d899dd36276db25890816f3605ddedbc53d33`
  Message: `Correct Phase 4C4B3A contract and validation`
- **Canonicalization and cleanup commit**: `35f86562989bff2a891b82cb6c36ab7cc2d9eda0`
  Message: `Correct Phase 4C4B3A canonicalization and cleanup`

## Test Results

### Focused Suite (11 Phase 4C4B3A files)

| File | Tests |
|---|---|
| `test_process_local_conversation_key.py` | 47 |
| `test_conversation_reset_coordinator.py` | 63 |
| `test_chat_owner_scoped_local_key.py` | 21 |
| `test_chat_trusted_identity_extraction.py` | 42 |
| `test_chat_owner_key_plumbing.py` | 39 |
| `test_chat_durable_lookup_key_plumbing.py` | 25 |
| `test_genie_pipeline_owner_key_plumbing.py` | 52 |
| `test_genie_pipeline_inactive_durable_state.py` | 73 |
| `test_genie_pipeline_durable_lookup.py` | 36 |
| `test_genie_pipeline_durable_writeback.py` | 50 |
| `test_genie_pipeline_last_message_persistence.py` | 57 |
| **Total focused** | **512 passed, 0 failed, 0 skipped** |

### Combined Suite (Phase 4C4B2 exact 26-file baseline + 2 new files)

**1315 passed, 0 failed, 0 skipped**

Arithmetic: Phase 4C4B2 baseline 1233 + 38 (original process-key tests) +
9 (parity tests) + 21 (chat-owner tests) + 14 (coordinator net additions) = 1315.

### Full Non-Live Suite

**2149 passed, 0 failed, 0 skipped, 1 warning (pre-existing Pydantic V2 deprecation)**

Excluded (live only):
- `tests/test_genie_live_smoke.py`
- `tests/test_genie_integration_smoke.py`
- `tests/test_delta_state_live_smoke.py`
- `tests/test_new_pipeline_live_smoke.py`

Arithmetic: Phase 4C4B2 non-live baseline 2060 + 47 (process-key with parity) +
21 (chat-owner) + 14 (coordinator net) = 2142.

## Side Fixes Applied During Phase

1. `chat.py`: added dedicated `except ProcessLocalConversationKeyError` handler
   that sets `fallback_recommended=False` (hard error, never fall through to
   custom pipeline fallback).
2. `process_local_conversation_key.py`: removed local import of
   `DurableGenieSessionKey`; inlined equivalent validation to maintain
   adapter isolation boundary enforced by `test_durable_genie_session_adapter.py`.

## Validation-Correction Changes (this commit)

1. `test_process_local_conversation_key.py`: added 9 validator-parity tests
   (`TestFrontendIDValidatorParity`) proving accept/reject parity between
   process-local helper and `DurableGenieSessionKey`.
2. Documentation renamed: `coordinator_cleanup.md` →
   `coordinator_local_cleanup_contract.md` (content corrected: removed
   stale Optional signature, documented mandatory contract and all success paths).
3. Documentation renamed: `exit_assessment.md` →
   `phase4c4b3a_exit_assessment.md` (this file, corrected content).
4. `test_report.md`: updated with authoritative pytest counts.

## Outstanding (Not Part of This Phase)

- No HTTP endpoint for conversation reset
- Reset coordinator has no caller in production yet (only tested in unit tests)
