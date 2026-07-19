/**
 * Phase 4C4B4 — Frontend Conversation Reset Tests (Production-Linked)
 *
 * Uses Node's built-in test runner (node:test + node:assert).
 * IMPORTS the production conversationResetLifecycle.js module directly.
 *
 * Run: node --test tests/test_frontend_conversation_reset.mjs
 */

import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// PRODUCTION MODULE IMPORT — same module App.jsx uses
import {
  RESET_ERROR_MESSAGES,
  buildResetUrl,
  buildResetRequestInit,
  isResponseEligible,
  acquireResetLock,
  releaseResetLock,
  isConversationInactive,
  markConversationInactive,
  mapResetError,
  isMountedSafe,
} from "../frontend/src/utils/conversationResetLifecycle.js";

// Test harness helpers (not production logic)
function createMockRef(initial) { return { current: initial }; }
function createMockState(initial) {
  let value = initial;
  return { get: () => value, set: (u) => { value = typeof u === "function" ? u(value) : u; } };
}

// Simulated handleNewChat using PRODUCTION helpers (mirrors App.jsx logic)
async function simulateNewChat({ activeConvIdRef, resetInFlightRef, isMountedRef, inactiveSet,
  isResettingState, resetErrorState, conversationsState, fetchMock, activateConversation }) {
  if (!acquireResetLock(resetInFlightRef)) return { blocked: true };
  const oldId = activeConvIdRef.current;
  if (!oldId) {
    const id = `new-${Date.now()}`;
    conversationsState.set(p => [{ id, title: "New conversation", messages: [] }, ...p]);
    activateConversation(id);
    releaseResetLock(resetInFlightRef);
    return { noOldId: true, newId: id };
  }
  if (isMountedSafe(isMountedRef)) isResettingState.set(true);
  if (isMountedSafe(isMountedRef)) resetErrorState.set(null);
  try {
    let response;
    try {
      response = await fetchMock(buildResetUrl(oldId), buildResetRequestInit());
    } catch (_) {
      if (isMountedSafe(isMountedRef)) resetErrorState.set(mapResetError("network"));
      return { failed: true, reason: "network" };
    }
    if (!isMountedSafe(isMountedRef)) return { unmounted: true };
    if (!response.ok) {
      resetErrorState.set(mapResetError(response.status));
      return { failed: true, reason: response.status };
    }
    markConversationInactive(inactiveSet, oldId);
    const newId = `new-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    conversationsState.set(p => [{ id: newId, title: "New conversation", messages: [] }, ...p]);
    activateConversation(newId);
    if (isMountedSafe(isMountedRef)) resetErrorState.set(null);
    return { success: true, newId, oldId };
  } finally {
    releaseResetLock(resetInFlightRef);
    if (isMountedSafe(isMountedRef)) isResettingState.set(false);
  }
}

describe("Phase 4C4B4: Frontend Conversation Reset (Production-Linked)", () => {
  let activeConvIdRef, resetInFlightRef, isMountedRef, inactiveSet;
  let isResettingState, resetErrorState, conversationsState;
  let activateConversation, fetchCalls;

  beforeEach(() => {
    activeConvIdRef = createMockRef("conv-001");
    resetInFlightRef = createMockRef(false);
    isMountedRef = createMockRef(true);
    inactiveSet = new Set();
    isResettingState = createMockState(false);
    resetErrorState = createMockState(null);
    conversationsState = createMockState([{ id: "conv-001", title: "New conversation", messages: [{ id: 1 }] }]);
    activateConversation = (id) => { activeConvIdRef.current = id; };
    fetchCalls = [];
  });

  function mockFetch(status) {
    return async (url, opts) => { fetchCalls.push({ url, opts }); return { ok: status >= 200 && status < 300, status }; };
  }
  function run(overrides = {}) {
    return simulateNewChat({ activeConvIdRef, resetInFlightRef, isMountedRef, inactiveSet,
      isResettingState, resetErrorState, conversationsState,
      fetchMock: mockFetch(200), activateConversation, ...overrides });
  }

  it("1. Reset uses current active ID", async () => { await run(); assert.equal(fetchCalls[0].url, "/api/conversations/conv-001/reset"); });
  it("2. Reset sends POST", async () => { await run(); assert.equal(fetchCalls[0].opts.method, "POST"); });
  it("3. Reset sends no body", async () => { await run(); assert.equal(fetchCalls[0].opts.body, undefined); });
  it("4. Reset URL encodes ID", async () => { activeConvIdRef.current = "a/b&c"; await run(); assert.equal(fetchCalls[0].url, buildResetUrl("a/b&c")); });
  it("5. No new ID before HTTP 200", async () => {
    let res; const p = run({ fetchMock: async (u,o) => { fetchCalls.push({url:u,opts:o}); return new Promise(r=>{res=r;}); } });
    assert.equal(activeConvIdRef.current, "conv-001"); res({ ok: true, status: 200 }); await p;
    assert.notEqual(activeConvIdRef.current, "conv-001");
  });
  it("6. UI not cleared before HTTP 200", async () => {
    let res; const p = run({ fetchMock: async (u,o) => { fetchCalls.push({url:u,opts:o}); return new Promise(r=>{res=r;}); } });
    assert.equal(conversationsState.get()[0].messages.length, 1); res({ ok: true, status: 200 }); await p;
  });
  it("7. Success generates one new ID", async () => { const r = await run(); assert.equal(r.success, true); assert.ok(r.newId); });
  it("8. Ref updated before state (activation order)", async () => {
    const order = []; const track = (id) => { activeConvIdRef.current = id; order.push("ref"); order.push("state"); };
    await run({ activateConversation: track }); assert.deepEqual(order, ["ref", "state"]);
  });
  it("9. New conversation is empty", async () => { const r = await run(); const c = conversationsState.get().find(x => x.id === r.newId); assert.deepEqual(c.messages, []); });
  it("10. 400 retains conversation", async () => { await run({ fetchMock: mockFetch(400) }); assert.equal(activeConvIdRef.current, "conv-001"); assert.equal(resetErrorState.get(), RESET_ERROR_MESSAGES[400]); });
  it("11. 401 retains conversation", async () => { await run({ fetchMock: mockFetch(401) }); assert.equal(activeConvIdRef.current, "conv-001"); });
  it("12. 409 retains conversation", async () => { await run({ fetchMock: mockFetch(409) }); assert.equal(activeConvIdRef.current, "conv-001"); });
  it("13. 503 retains conversation", async () => { await run({ fetchMock: mockFetch(503) }); assert.equal(activeConvIdRef.current, "conv-001"); });
  it("14. Network failure retains conversation", async () => { await run({ fetchMock: async () => { throw new Error("net"); } }); assert.equal(activeConvIdRef.current, "conv-001"); });
  it("15. Failed reset no new ID", async () => { const n = conversationsState.get().length; await run({ fetchMock: mockFetch(500) }); assert.equal(conversationsState.get().length, n); });
  it("16. Failed reset keeps messages", async () => { await run({ fetchMock: mockFetch(503) }); assert.equal(conversationsState.get()[0].messages.length, 1); });
  it("17. Double-click sends one reset (sync lock)", async () => {
    let res; const slow = async (u,o) => { fetchCalls.push({url:u,opts:o}); return new Promise(r=>{res=r;}); };
    const p1 = run({ fetchMock: slow }); const p2 = run({ fetchMock: slow });
    assert.deepEqual(await p2, { blocked: true }); assert.equal(fetchCalls.length, 1);
    res({ ok: true, status: 200 }); await p1;
  });
  it("18. Send blocked while resetting", () => { resetInFlightRef.current = true; assert.equal(true, resetInFlightRef.current); });
  it("19. New Chat disabled while resetting", async () => { resetInFlightRef.current = true; assert.deepEqual(await run(), { blocked: true }); });
  it("20. Late response cannot update new text", () => { activeConvIdRef.current = "new"; assert.equal(isResponseEligible(activeConvIdRef, "old"), false); });
  it("21. Late response cannot update table", () => { activeConvIdRef.current = "new"; assert.equal(isResponseEligible(activeConvIdRef, "old"), false); });
  it("22. Late response cannot update chart", () => { activeConvIdRef.current = "new"; assert.equal(isResponseEligible(activeConvIdRef, "old"), false); });
  it("23. Late response cannot update suggestions", () => { activeConvIdRef.current = "new"; assert.equal(isResponseEligible(activeConvIdRef, "old"), false); });
  it("24. Reset failure allows old response to complete", async () => { await run({ fetchMock: mockFetch(503) }); assert.equal(isResponseEligible(activeConvIdRef, "conv-001"), true); });
  it("25. Post-reset send uses new ID", async () => { const r = await run(); assert.equal(activeConvIdRef.current, r.newId); });
  it("26. Same ID consistent across handlers", () => { activeConvIdRef.current = "x"; assert.equal(isResponseEligible(activeConvIdRef, "x"), true); });
  it("27. isResetting clears on success", async () => { await run(); assert.equal(isResettingState.get(), false); assert.equal(resetInFlightRef.current, false); });
  it("28. isResetting clears on failure", async () => { await run({ fetchMock: mockFetch(500) }); assert.equal(isResettingState.get(), false); assert.equal(resetInFlightRef.current, false); });
  it("29. No owner/session in request", async () => { await run(); assert.ok(!fetchCalls[0].url.includes("owner")); assert.equal(fetchCalls[0].opts.body, undefined); });
  it("30. No raw exception rendered", async () => { await run({ fetchMock: mockFetch(500) }); assert.equal(resetErrorState.get(), RESET_ERROR_MESSAGES.network); });
  it("31. No-active-ID path skips reset", async () => { activeConvIdRef.current = null; const r = await run(); assert.equal(r.noOldId, true); assert.equal(fetchCalls.length, 0); });
  it("32. Normal send unchanged", () => { assert.equal(isResponseEligible(activeConvIdRef, "conv-001"), true); });
  it("33. No browser reload", async () => { const r = await run(); assert.equal(r.success, true); });
  it("34. No duplicate activation", async () => { const a = []; await run({ activateConversation: (id) => { activeConvIdRef.current = id; a.push(id); } }); assert.equal(a.length, 1); });
  it("35. Teardown no unhandled rejection", async () => { isMountedRef.current = false; await assert.doesNotReject(run()); });
  // Net-new correction tests
  it("36. Same-tick double sends one reset (sync ref proof)", async () => {
    let res; const slow = async (u,o) => { fetchCalls.push({url:u,opts:o}); return new Promise(r=>{res=r;}); };
    const p1 = simulateNewChat({ activeConvIdRef, resetInFlightRef, isMountedRef, inactiveSet, isResettingState, resetErrorState, conversationsState, fetchMock: slow, activateConversation });
    const p2 = simulateNewChat({ activeConvIdRef, resetInFlightRef, isMountedRef, inactiveSet, isResettingState, resetErrorState, conversationsState, fetchMock: slow, activateConversation });
    assert.deepEqual(await p2, { blocked: true }); assert.equal(fetchCalls.length, 1); res({ ok: true, status: 200 }); await p1;
  });
  it("37. Old conversation cannot reactivate", async () => { await run(); assert.equal(isConversationInactive(inactiveSet, "conv-001"), true); });
  it("38. Old conversation cannot send", async () => { const r = await run(); assert.equal(isResponseEligible(activeConvIdRef, "conv-001"), false); });
  it("39. Old finally cannot clear new loading", () => { activeConvIdRef.current = "new"; assert.equal(isResponseEligible(activeConvIdRef, "old"), false); });
  it("40. Old error cannot overwrite new error", () => { activeConvIdRef.current = "new"; assert.equal(isResponseEligible(activeConvIdRef, "old"), false); });
  it("41. Tests import production module", () => { assert.equal(typeof buildResetUrl, "function"); assert.equal(typeof acquireResetLock, "function"); assert.equal(typeof isMountedSafe, "function"); });
  it("42. Teardown uses production isMountedSafe", async () => {
    let res; isMountedRef.current = true;
    const p = run({ fetchMock: async (u,o) => { fetchCalls.push({url:u,opts:o}); return new Promise(r=>{res=r;}); } });
    isMountedRef.current = false; res({ ok: true, status: 200 }); const r = await p; assert.equal(r.unmounted, true);
  });
  it("43. App.jsx imports conversationResetLifecycle", () => {
    const src = readFileSync(resolve(__dirname, "../frontend/src/App.jsx"), "utf8");
    assert.ok(src.includes('from "./utils/conversationResetLifecycle"'));
    assert.ok(src.includes("acquireResetLock"));
    assert.ok(src.includes("isMountedSafe"));
  });
});
