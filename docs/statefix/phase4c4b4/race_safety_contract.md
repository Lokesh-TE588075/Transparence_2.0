# Phase 4C4B4 — Race Safety Contract (Corrected)

## Synchronous Reset Lock

| Mechanism | Layer | Purpose |
|-----------|-------|---------|
| `resetInFlightRef` | useRef (sync) | Prevents same-tick double invocation |
| `isResetting` state | useState (async) | UI indicator only |
| `acquireResetLock()` | Production helper | Returns false if already locked |
| `releaseResetLock()` | Production helper | Always called in finally |

### Same-Tick Guarantee
Two synchronous calls to `handleNewChat` within the same event-loop tick:
1. First call: `acquireResetLock(resetInFlightRef)` → true → proceeds
2. Second call: `acquireResetLock(resetInFlightRef)` → false → returns immediately
3. No await has occurred — lock is purely synchronous

## Response Eligibility

Every async callback checks `isResponseEligible(activeConvIdRef, requestConversationId)`:
- Success path (botMsg): guard before state update
- Error path (catch): guard before error message append
- Finally path: guard before `setIsLoading(false)`

## Inactive Conversation Enforcement

After successful reset:
1. `markConversationInactive(inactiveConvIdsRef.current, oldId)` adds to Set
2. `handleSelectConversation` checks `isConversationInactive()` before activation
3. Old conversation remains visible in sidebar (read-only) but cannot be reactivated

## Component Teardown

`isMountedRef.current = false` on useEffect cleanup.
All state updates in handleNewChat wrapped with `isMountedSafe(isMountedRef)`.
handleSendMessage finally checks `isMountedSafe(isMountedRef)` before clearing loading.
