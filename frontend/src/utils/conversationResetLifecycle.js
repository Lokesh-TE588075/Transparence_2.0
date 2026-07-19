/**
 * conversationResetLifecycle.js
 *
 * Production-linked pure lifecycle helpers for the conversation reset workflow.
 * Imported by App.jsx (production) and test_frontend_conversation_reset.mjs (tests).
 * Contains NO React or DOM dependencies — pure ES module.
 */

// ============================================================
// Error mapping (Step 7)
// ============================================================

export const RESET_ERROR_MESSAGES = Object.freeze({
  400: "The current conversation could not be reset.",
  401: "Your session could not be verified. Please refresh and try again.",
  409: "The conversation could not be reset because it changed. Please try again.",
  503: "Conversation reset is temporarily unavailable. Please try again.",
  network: "Conversation reset is temporarily unavailable. Please try again.",
});

// ============================================================
// Reset request construction (Step 6)
// ============================================================

/**
 * Build the reset endpoint URL with URL-encoded conversation ID.
 * @param {string} frontendConversationId
 * @returns {string}
 */
export function buildResetUrl(frontendConversationId) {
  const encoded = encodeURIComponent(frontendConversationId);
  return `/api/conversations/${encoded}/reset`;
}

/**
 * Build the fetch options for the reset request.
 * No body, no extra headers, same-origin credentials.
 * @returns {RequestInit}
 */
export function buildResetRequestInit() {
  return { method: "POST", credentials: "same-origin" };
}

// ============================================================
// Active-response eligibility check (Step 3 / Step 9)
// ============================================================

/**
 * Returns true if a response should be applied to the active conversation.
 * Must be called before ANY state update from an async response.
 * @param {{ current: string|null }} activeConvIdRef - mutable ref
 * @param {string} requestConversationId - captured at request time
 * @returns {boolean}
 */
export function isResponseEligible(activeConvIdRef, requestConversationId) {
  return activeConvIdRef.current === requestConversationId;
}

// ============================================================
// Synchronous reset lock (Step 4)
// ============================================================

/**
 * Attempt to acquire the reset lock.
 * Returns true if lock acquired (caller should proceed).
 * Returns false if already locked (caller should bail).
 * @param {{ current: boolean }} resetInFlightRef
 * @returns {boolean}
 */
export function acquireResetLock(resetInFlightRef) {
  if (resetInFlightRef.current) return false;
  resetInFlightRef.current = true;
  return true;
}

/**
 * Release the reset lock.
 * @param {{ current: boolean }} resetInFlightRef
 */
export function releaseResetLock(resetInFlightRef) {
  resetInFlightRef.current = false;
}

// ============================================================
// Conversation inactivity check (Step 5 / Step 10)
// ============================================================

/**
 * Check if a conversation ID is in the inactive (reset/tombstoned) set.
 * @param {Set<string>} inactiveSet
 * @param {string} conversationId
 * @returns {boolean}
 */
export function isConversationInactive(inactiveSet, conversationId) {
  return inactiveSet.has(conversationId);
}

/**
 * Mark a conversation as inactive after successful reset.
 * @param {Set<string>} inactiveSet
 * @param {string} conversationId
 */
export function markConversationInactive(inactiveSet, conversationId) {
  inactiveSet.add(conversationId);
}

// ============================================================
// HTTP status mapping helper
// ============================================================

/**
 * Map an HTTP status code (or 'network') to a sanitized user-facing message.
 * Never exposes raw backend errors.
 * @param {number|string} statusOrKey
 * @returns {string}
 */
export function mapResetError(statusOrKey) {
  return RESET_ERROR_MESSAGES[statusOrKey] || RESET_ERROR_MESSAGES.network;
}

// ============================================================
// Mounted-safe state update guard (Step 7 - teardown)
// ============================================================

/**
 * Returns true if component is still mounted and safe to update state.
 * @param {{ current: boolean }} isMountedRef
 * @returns {boolean}
 */
export function isMountedSafe(isMountedRef) {
  return isMountedRef.current === true;
}
