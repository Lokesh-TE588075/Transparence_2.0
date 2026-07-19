import React, { useState, useEffect, useRef, useCallback } from "react";
import Header from "./components/Header";
import Sidebar from "./components/Sidebar";
import ChatWindow from "./components/ChatWindow";
import HelpModal from "./components/HelpModal";
import FeedbackModal from "./components/FeedbackModal";
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
} from "./utils/conversationResetLifecycle";

// Generate a unique conversation ID.
function _newConvId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// Call the backend reset endpoint using production helpers.
async function resetConversation(frontendConversationId) {
  const url = buildResetUrl(frontendConversationId);
  const init = buildResetRequestInit();
  return fetch(url, init);
}

export default function App() {
  const _initialId = _newConvId();
  const [conversations, setConversations] = useState([
    { id: _initialId, title: "New conversation", messages: [] }
  ]);
  const [activeConvId, setActiveConvId] = useState(_initialId);
  const [isLoading, setIsLoading] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [resetError, setResetError] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [showHelp, setShowHelp] = useState(false);
  const [showFeedback, setShowFeedback] = useState(false);

  // Step 3: Immediately mutable ref for race-safe async callbacks.
  const activeConvIdRef = useRef(_initialId);

  // Step 3: Single activation function — ref first, then reactive state.
  const activateConversation = useCallback((nextId) => {
    activeConvIdRef.current = nextId;
    setActiveConvId(nextId);
  }, []);

  // Step 5: Guard sidebar selection — inactive conversations are read-only.
  const handleSelectConversation = useCallback((id) => {
    if (isConversationInactive(inactiveConvIdsRef.current, id)) return;
    activateConversation(id);
  }, [activateConversation]);

  const activeConv = conversations.find(c => c.id === activeConvId) || conversations[0];

  // Step 5: Reset-first New Chat flow with synchronous lock (Step 4).
  const handleNewChat = useCallback(async () => {
    // Step 4: Synchronous lock — prevents same-tick double invocation.
    if (!acquireResetLock(resetInFlightRef)) return;

    const oldConversationId = activeConvIdRef.current;

    // If no active conversation exists (fresh app, edge case), just create one.
    if (!oldConversationId) {
      const id = _newConvId();
      const newConv = { id, title: "New conversation", messages: [] };
      setConversations(prev => [newConv, ...prev]);
      activateConversation(id);
      releaseResetLock(resetInFlightRef);
      return;
    }

    if (isMountedSafe(isMountedRef)) setIsResetting(true);
    if (isMountedSafe(isMountedRef)) setResetError(null);

    try {
      let response;
      try {
        response = await resetConversation(oldConversationId);
      } catch (_networkErr) {
        if (isMountedSafe(isMountedRef)) setResetError(mapResetError("network"));
        return;
      }

      if (!isMountedSafe(isMountedRef)) return;

      if (!response.ok) {
        setResetError(mapResetError(response.status));
        return;
      }

      // Step 5/10: Mark old conversation inactive.
      markConversationInactive(inactiveConvIdsRef.current, oldConversationId);

      // Success: generate new ID, activate, clear conversation UI (Step 8).
      const newId = _newConvId();
      const newConv = { id: newId, title: "New conversation", messages: [] };
      setConversations(prev => [newConv, ...prev]);
      activateConversation(newId);
      setResetError(null);
    } finally {
      releaseResetLock(resetInFlightRef);
      if (isMountedSafe(isMountedRef)) setIsResetting(false);
    }
  }, [activateConversation]);

  const handleDeleteConversation = (id) => {
    setConversations(prev => {
      const filtered = prev.filter(c => c.id !== id);
      if (filtered.length === 0) {
        const fresh = { id: _newConvId(), title: "New conversation", messages: [] };
        setActiveConvId(fresh.id);
        return [fresh];
      }
      if (activeConvId === id) setActiveConvId(filtered[0].id);
      return filtered;
    });
  };

  // Step 9D: Block message submission while resetting.
  const handleSendMessage = async (text) => {
    if (!text.trim() || isLoading || resetInFlightRef.current) return;

    // Step 3: Capture active conversation ID at request time.
    const requestConversationId = activeConvIdRef.current;

    const userMsg = { id: Date.now(), role: "user", content: text, timestamp: new Date() };
    setConversations(prev => prev.map(c =>
      c.id === requestConversationId ? { ...c, messages: [...c.messages, userMsg] } : c
    ));
    setIsLoading(true);
    // Clear any previous reset error on new send.
    setResetError(null);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: requestConversationId }),
      });
      const data = await res.json();

      // Step 3 + Step 9: Guard — only update if this conversation is still active.
      if (!isResponseEligible(activeConvIdRef, requestConversationId)) {
        // Late response from old conversation — discard silently.
        return;
      }

      const botMsg = {
        id: Date.now() + 1, role: "assistant", content: data.message,
        status: data.status, isTable: data.is_table, tableData: data.table_data,
        rowCount: data.row_count,
          previewRowCount: data.preview_row_count ?? 0,
          returnedRowCount: data.returned_row_count ?? data.row_count,
          totalRowCount: data.total_row_count ?? null,
          exportRowCount: data.export_row_count ?? null,
          displayRowLimit: data.display_row_limit ?? 100,
          downloadKey: data.download_key,
          exportId: data.export_id || null,
          exportStatus: data.export_status || null,
          exportMode: data.export_mode || null,
        executionTime: data.execution_time_ms, timestamp: new Date(),
        // Genie extension fields (G6/Q2) — null/undefined for non-Genie responses;
        // ChatWindow checks each field before rendering its component.
        source: data.source || null,
        suggestedQuestions:
          data.suggested_questions?.length ? data.suggested_questions : null,
        generatedSql: data.generated_sql || null,
        hasVisualization: data.has_visualization || false,
        genieConversationId: data.genie_conversation_id || null,
        genieMessageId: data.genie_message_id || null,
        // Q2: context line — short plain-English label for the query executed
        queryDescription: data.query_description || null,
        // Q3: computed chart data from table summarizer (fallback when Genie returns no viz)
        computedChartData: data.computed_chart_data || null,
        computedMetrics: data.computed_metrics || null,
      };

      setConversations(prev => prev.map(c => {
        if (c.id !== requestConversationId) return c;
        const updated = { ...c, messages: [...c.messages, botMsg] };
        if (c.messages.length === 0 || c.title === "New conversation") {
          updated.title = text.slice(0, 35) + (text.length > 35 ? "..." : "");
        }
        return updated;
      }));
    } catch (err) {
      // Step 9B: Guard on error path — stale error must not overwrite new conversation.
      if (!isResponseEligible(activeConvIdRef, requestConversationId)) return;
      const errMsg = { id: Date.now() + 1, role: "assistant", content: "I'm having trouble connecting. Please try again in a moment.", status: "error", timestamp: new Date() };
      setConversations(prev => prev.map(c =>
        c.id === requestConversationId ? { ...c, messages: [...c.messages, errMsg] } : c
      ));
    } finally {
      // Step 6/9: Only clear loading if still active and mounted.
      if (isResponseEligible(activeConvIdRef, requestConversationId) && isMountedSafe(isMountedRef)) {
        setIsLoading(false);
      }
    }
  };

  const handleFeedback = async (messageId, rating) => {
    try {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rating, conversation_id: activeConvId, message_id: String(messageId) }),
      });
      setConversations(prev => prev.map(c =>
        c.id === activeConvId ? {
          ...c, messages: c.messages.map(m => m.id === messageId ? { ...m, feedback: rating } : m)
        } : c
      ));
    } catch (err) { console.error("Feedback error:", err); }
  };

  return (
    <div className="app">
      <Header
        onToggleSidebar={() => setSidebarOpen(!sidebarOpen)}
        onHelp={() => setShowHelp(true)}
        onFeedback={() => setShowFeedback(true)}
      />
      <div className="app-body">
        <Sidebar
          conversations={conversations}
          activeId={activeConvId}
          onSelect={handleSelectConversation}
          onNew={handleNewChat}
          onDelete={handleDeleteConversation}
          isOpen={sidebarOpen}
          isResetting={isResetting}
        />
        <main className={`main-content ${sidebarOpen ? "" : "expanded"}`}>
          {resetError && (
            <div className="reset-error-banner" role="alert">
              <span>{resetError}</span>
              <button onClick={() => setResetError(null)} aria-label="Dismiss error">&times;</button>
            </div>
          )}
          {isResetting && (
            <div className="resetting-indicator" role="status" aria-live="polite">
              Resetting conversation…
            </div>
          )}
          <ChatWindow
            conversation={activeConv}
            isLoading={isLoading || isResetting}
            onSend={handleSendMessage}
            onFeedback={handleFeedback}
          />
        </main>
      </div>
      {showHelp && <HelpModal onClose={() => setShowHelp(false)} />}
      {showFeedback && <FeedbackModal onClose={() => setShowFeedback(false)} conversationId={activeConvId} />}
    </div>
  );
}
