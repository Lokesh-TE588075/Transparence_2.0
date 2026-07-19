# Test Coverage — Phase 4C4B3B

## Test Files

### `tests/test_conversation_reset_route.py` (31 tests)

| Class | Tests | Description |
|-------|-------|-------------|
| `TestCoreResetBehaviour` | 01–06 | Real coordinator + in-memory repo: ACTIVE, RESET, STALE, EXPIRED, missing, repeated |
| `TestIdentityFailureContract` | 07–10b | ResolutionError→401, ConfigurationError→503, None→503, bad/empty frontend_id→400 |
| `TestCoordinatorErrorMapping` | 11–14 | Conflict→409, Unavailable→503, InternalError→503, RuntimeError→503 static |
| `TestOwnerIsolationAndOverrideRejection` | 15–19 | Body/query/header cannot override owner; same frontend_id different owners isolated; same cookie different owners produce different local keys |
| `TestCoordinatorInvocationContract` | 20–22 | Route passes canonical ID, exact owner hash, valid plc_v1_ key |
| `TestOutputHygiene` | 23–25 | No owner hash / session ID / frontend ID in success body, error body, or logs |
| `TestSharedInstanceInvariants` | 26–30 | No duplicate store/adapter constructed; no Genie request; no fallback; no adapter.delete() called |

### `tests/test_conversation_reset_runtime_wiring.py` (10 tests)

| Test | Description |
|------|-------------|
| 01 | Chat and reset use the same `GenieSessionStore` object (shared singleton) |
| 02 | Owner A reset does not affect Owner B session in shared store |
| 03 | Owner isolation propagates through the shared adapter |
| 04 | Coordinator holds the exact adapter from the pipeline bundle |
| 05 | Importing `app.main` does not open any network/database connection |
| 06 | `conversation_reset_runtime` uses deferred imports only |
| 07 | Shutdown lifecycle calls `reset_genie_pipeline()` inside a `finally` block |
| 08 | `get_conversation_reset_coordinator()` raises `ConversationResetRuntimeUnavailableError` when pipeline not ready |
| 09 | Reset router is registered exactly once in `app.main` |
| 10 | Existing `/api/chat` route remains registered (no regressions) |

## Test Strategy Notes

- Tests 01–06 and 01–04 (wiring) use real `InMemoryConversationRepository`,
  `DurableGenieSessionAdapter`, `GenieSessionStore`, and
  `ConversationResetCoordinator`. Only `get_conversation_reset_coordinator()`
  and `resolve_request_owner_identity()` are monkeypatched.
- The async route function is tested synchronously via `asyncio.run()` (safe
  for cross-test isolation with `--asyncio-mode=auto`).
- Wiring tests patch `app.services.genie_backend_factory.get_genie_pipeline`
  (the deferred-import target) rather than any module-level attribute on
  `conversation_reset_runtime`.

## Baseline

| Metric | Value |
|--------|-------|
| Phase 4C4B3A baseline | 2149 tests |
| New tests added | +41 |
| Phase 4C4B3B final | **2190 tests** |
| Failures | 0 |
| Skipped | 0 |
