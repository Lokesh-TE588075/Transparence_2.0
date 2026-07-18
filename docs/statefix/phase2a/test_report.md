# Phase 2A Test Report

Phase 2A — Conversation Repository Contract, Domain Model,
In-Memory Reference Implementation, and Unit Tests

Date: 2026-07-18

---

## 1. New Repository Tests

**File:** `tests/test_conversation_repository.py`

**Command:**
```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q tests/test_conversation_repository.py
```

**Result: 87/87 PASSED (0.89 s)**

### Test breakdown by category

| Test class | Tests | Result |
|------------|-------|--------|
| `TestDomainModel` | 12 | All pass |
| `TestCreation` | 10 | All pass |
| `TestLookup` | 7 | All pass |
| `TestGenieBinding` | 7 | All pass |
| `TestMessageUpdate` | 4 | All pass |
| `TestTouch` | 6 | All pass |
| `TestStatus` | 7 | All pass |
| `TestCompareAndUpdate` | 7 | All pass |
| `TestListing` | 9 | All pass |
| `TestDeletion` | 8 | All pass |
| `TestThreadSafety` | 3 | All pass |
| `TestInterface` | 7 | All pass |
| **TOTAL** | **87** | **87 PASS** |

---

## 2. Relevant Regression Tests

**Files:** `test_genie_session_store.py`, `test_genie_session_store_context.py`,
`test_multi_user_session_isolation.py`

**Command:**
```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q \
  tests/test_genie_session_store.py \
  tests/test_genie_session_store_context.py \
  tests/test_multi_user_session_isolation.py
```

**Result:** 49 passed, 11 pre-existing failures

**Pre-existing failures:** 11 tests in `TestServerConversationKeyComposition`,
`TestExportIsolation`, and `TestUserHeaderExtraction` (all in
`test_multi_user_session_isolation.py`) fail with
`ModuleNotFoundError: No module named 'pydantic_settings'`.
This is a serverless notebook environment issue where the production dependency
`pydantic_settings` is not pre-installed.  These failures exist on the baseline
branch and are not caused by Phase 2A.

---

## 3. Full Non-Live Suite

**Command:**
```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q \
  --continue-on-collection-errors \
  --ignore=tests/test_genie_live_smoke.py \
  --ignore=tests/test_genie_integration_smoke.py \
  --ignore=tests/test_delta_state_live_smoke.py \
  --ignore=tests/test_new_pipeline_live_smoke.py
```

**Result:** 772 passed, 9 collection errors

### Collection errors (all pre-existing)

| File | Root cause |
|------|------------|
| `test_chat_pipeline.py` | `ModuleNotFoundError: rapidfuzz` (not installed in serverless) |
| `test_conversation_followups.py` | `ModuleNotFoundError: rapidfuzz` |
| `test_conversation_repository.py` | App namespace pollution (see note below) |
| `test_conversation_state_cleanup.py` | `ModuleNotFoundError: rapidfuzz` |
| `test_conversation_state_factory.py` | `ModuleNotFoundError: rapidfuzz` |
| `test_delivery_date_normalizer.py` | `ModuleNotFoundError: rapidfuzz` |
| `test_delta_conversation_state.py` | `ModuleNotFoundError: rapidfuzz` |
| `test_deterministic_followup.py` | `ModuleNotFoundError: rapidfuzz` |
| `test_input_normalizer.py` | `ModuleNotFoundError: rapidfuzz` |

**Note on `test_conversation_repository.py` in full-suite collection:**
When pytest collects all test files together in alphabetical order,
`test_chat_pipeline.py` is collected before `test_conversation_repository.py`.
That file imports `app.services.chat_pipeline`, which in the serverless notebook
environment resolves to `../transparence_app/app/services/chat_pipeline.py`
(a separate legacy project on the system path).  This poisons `sys.modules['app']`
with the `../transparence_app/app/` package.  When my test is subsequently
collected, `from app.services.conversation_repository import ...` fails because
`sys.modules['app']` now points to the wrong project directory.

**This is an environment artifact, not a defect in the Phase 2A code.**
Confirmation: running `test_conversation_repository.py` in isolation gives **87/87 PASS**.
Running it first in a combined invocation (before the poisoning files) also gives **87 PASS**.

---

## 4. Excluded Live Tests

The following four files were excluded from all runs:

- `tests/test_genie_live_smoke.py`
- `tests/test_genie_integration_smoke.py`
- `tests/test_delta_state_live_smoke.py`
- `tests/test_new_pipeline_live_smoke.py`

These require a live Databricks warehouse and Genie Space and are never run in CI.

---

## 5. Baseline Comparison

| Metric | Baseline (apps container) | Phase 2A (serverless notebook) |
|--------|--------------------------|--------------------------------|
| Non-live tests passing | 911 | 772 (no rapidfuzz/app-namespace) + 87 new (isolated) |
| New Phase 2A tests | 0 | 87 |
| Pre-existing collection errors | 0 (full deps present) | 9 (env limitations) |
| Failures caused by Phase 2A | n/a | 0 |

---

## 6. Dependency Audit

The production module `app/services/conversation_repository.py` imports only:

- `__future__` (annotations)
- `threading` (RLock)
- `uuid` (UUID4 generation)
- `dataclasses` (dataclass, replace)
- `datetime` (datetime, timezone)
- `enum` (Enum)
- `typing` (Collection, Dict, List, Optional, Protocol, Tuple, runtime_checkable)

All are Python standard library modules.  No new entry was added to `requirements.txt`.
No Lakebase, psycopg, or SQLAlchemy import exists anywhere in the file.
