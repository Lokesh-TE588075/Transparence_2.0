# Phase 4C4B4 — Frontend Reset Contract

## Implementation SHA
- Original: `384b610d596a7ca491ceff4db03813a9874f513c`
- Final correction: `c20b9357e6ba72916df9cb6f7e73bc5066fb72d8`

## Contract Summary

### Reset Flow (handleNewChat)
1. `acquireResetLock(resetInFlightRef)` — synchronous, before first await
2. `setIsResetting(true)` — only when mounted (`isMountedSafe(isMountedRef)`)
3. Capture `oldConversationId = activeConvIdRef.current`
4. Call backend: `POST /api/conversations/{id}/reset`
5. Await HTTP 200
6. Mark old ID inactive: `markConversationInactive(inactiveConvIdsRef, oldId)`
7. Remove old conversation from selectable list: `prev.filter(item => item.id !== oldConversationId)`
8. Generate one new ID: `_newConvId()`
9. Synchronously update ref: `activeConvIdRef.current = newId`
10. Update reactive state: `setActiveConvId(newId)`
11. Release lock in `finally`: `releaseResetLock(resetInFlightRef)`

### Mounted-Ref Lifecycle (StrictMode-safe)
```javascript
const isMountedRef = useRef(true);
useEffect(() => {
    isMountedRef.current = true;
    return () => { isMountedRef.current = false; };
}, []);
```
Explicitly re-sets `true` on mount for React 18 StrictMode remount behavior.

### Synchronous Lock Ordering
- `resetInFlightRef = useRef(false)` — declared before any useCallback
- `acquireResetLock()` is the first statement in handleNewChat (before any await)
- Prevents same-tick double invocations even before React re-renders

### Old-Conversation Removal Policy
After successful reset (HTTP 200):
- Old conversation ID is removed from `conversations` state array
- Old ID is added to `inactiveConvIdsRef` (defence-in-depth)
- Old ID cannot appear in sidebar
- Old ID cannot be selected
- Old ID cannot be used for send (response eligibility guard)
- On failure: old conversation remains in list unchanged

### Response Eligibility Guards
Every async state setter checks BOTH:
- Component is mounted: `isMountedSafe(isMountedRef)`
- Response belongs to active conversation: `isResponseEligible(activeConvIdRef, requestConversationId)`

### Production Helper Module
`frontend/src/utils/conversationResetLifecycle.js` — exports:
- `RESET_ERROR_MESSAGES`, `buildResetUrl`, `buildResetRequestInit`
- `isResponseEligible`, `acquireResetLock`, `releaseResetLock`
- `isConversationInactive`, `markConversationInactive`
- `mapResetError`, `isMountedSafe`

Imported by both App.jsx and test_frontend_conversation_reset.mjs.
No duplicate implementations exist.
