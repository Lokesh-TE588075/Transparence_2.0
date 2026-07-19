# Phase 4C4B4 — Race Safety Contract

## Implementation SHA
- Original: `384b610d596a7ca491ceff4db03813a9874f513c`
- Final correction: TBD (this commit)

## Race Conditions Addressed

### 1. Same-Tick Double Invocation
- **Mechanism**: `resetInFlightRef = useRef(false)` + `acquireResetLock()`
- **Timing**: Synchronous check, before any await or state setter
- **Proof**: Tests 17, 36 — concurrent simulateNewChat calls, only one proceeds

### 2. Stale Response After Reset
- **Mechanism**: `isResponseEligible(activeConvIdRef, requestConversationId)`
- **Coverage**: Success path (line 153), error path (line 198), finally/loading (line 205)
- **Proof**: Tests 20-23, 38-40

### 3. Component Teardown During Async
- **Mechanism**: `isMountedRef` + `isMountedSafe()` before every state setter
- **Lifecycle**: StrictMode-safe useEffect (re-mounts set true, cleanup sets false)
- **Proof**: Tests 35, 42

### 4. Old Conversation Reactivation
- **Mechanism**: Old conv removed from list + inactive Set defence-in-depth
- **handleSelectConversation**: checks `isConversationInactive()` before activation
- **Proof**: Tests 37, 44, 46

### 5. Loading State Leak
- **Mechanism**: finally block checks `isResponseEligible` before `setIsLoading(false)`
- **Scenario**: Reset occurs during in-flight send → old finally cannot clear new loading
- **Proof**: Test 39

## Lock Ordering
```
acquireResetLock(resetInFlightRef)  ← synchronous, position 0
  ↓
isMountedSafe(isMountedRef) check
  ↓
setIsResetting(true)               ← first async-visible state change
  ↓
await resetConversation(...)       ← first await
  ↓
releaseResetLock(resetInFlightRef) ← always in finally
```

## Ref Declaration Order in App.jsx
```javascript
const activeConvIdRef = useRef(_initialId);       // line 49
const resetInFlightRef = useRef(false);           // line 52
const isMountedRef = useRef(true);                // line 56
// useEffect mounted lifecycle                    // lines 57-60
const inactiveConvIdsRef = useRef(new Set());     // line 63
```
All refs declared before any useCallback that references them.
