import React, { useState, useEffect } from "react";
import Header from "./components/Header";
import Sidebar from "./components/Sidebar";
import ChatWindow from "./components/ChatWindow";
import HelpModal from "./components/HelpModal";
import FeedbackModal from "./components/FeedbackModal";

// Generate a unique conversation ID.
// crypto.randomUUID() is available in all modern browsers (Chrome 92+, Firefox 95+,
// Safari 15.4+) and avoids the collision risk of hardcoded "1" or timestamp IDs.
// Fallback: timestamp + random suffix for older environments.
function _newConvId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function App() {
  const _initialId = _newConvId();
  const [conversations, setConversations] = useState([
    { id: _initialId, title: "New conversation", messages: [] }
  ]);
  const [activeConvId, setActiveConvId] = useState(_initialId);
  const [isLoading, setIsLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [showHelp, setShowHelp] = useState(false);
  const [showFeedback, setShowFeedback] = useState(false);

  const activeConv = conversations.find(c => c.id === activeConvId) || conversations[0];

  const handleNewChat = () => {
    const id = _newConvId();
    const newConv = { id, title: "New conversation", messages: [] };
    setConversations(prev => [newConv, ...prev]);
    setActiveConvId(id);
  };

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

  const handleSendMessage = async (text) => {
    if (!text.trim() || isLoading) return;

    const userMsg = { id: Date.now(), role: "user", content: text, timestamp: new Date() };
    setConversations(prev => prev.map(c =>
      c.id === activeConvId ? { ...c, messages: [...c.messages, userMsg] } : c
    ));
    setIsLoading(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: activeConvId }),
      });
      const data = await res.json();

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
        if (c.id !== activeConvId) return c;
        const updated = { ...c, messages: [...c.messages, botMsg] };
        if (c.messages.length === 0 || c.title === "New conversation") {
          updated.title = text.slice(0, 35) + (text.length > 35 ? "..." : "");
        }
        return updated;
      }));
    } catch (err) {
      const errMsg = { id: Date.now() + 1, role: "assistant", content: "I\'m having trouble connecting. Please try again in a moment.", status: "error", timestamp: new Date() };
      setConversations(prev => prev.map(c =>
        c.id === activeConvId ? { ...c, messages: [...c.messages, errMsg] } : c
      ));
    } finally {
      setIsLoading(false);
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
          onSelect={setActiveConvId}
          onNew={handleNewChat}
          onDelete={handleDeleteConversation}
          isOpen={sidebarOpen}
        />
        <main className={`main-content ${sidebarOpen ? "" : "expanded"}`}>
          <ChatWindow
            conversation={activeConv}
            isLoading={isLoading}
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
