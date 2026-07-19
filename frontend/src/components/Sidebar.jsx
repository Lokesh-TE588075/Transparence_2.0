import React from "react";

export default function Sidebar({ conversations, activeId, onSelect, onNew, onDelete, isOpen, isResetting }) {
  return (
    <aside className={`sidebar ${isOpen ? "open" : "closed"}`}>
      <div className="sidebar-top">
        <button className="new-chat-btn" onClick={onNew} disabled={isResetting}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M12 5v14M5 12h14"/>
          </svg>
          {isResetting ? "Resetting…" : "New Chat"}
        </button>
      </div>

      <nav className="conversation-list">
        {conversations.map((conv) => (
          <div
            key={conv.id}
            className={`conv-item ${conv.id === activeId ? "active" : ""}`}
            onClick={() => onSelect(conv.id)}
          >
            <svg className="conv-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            <span className="conv-title">{conv.title}</span>
            <button
              className="conv-delete"
              onClick={(e) => { e.stopPropagation(); onDelete(conv.id); }}
              title="Delete"
              aria-label="Delete conversation"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
        ))}
      </nav>

      <div className="sidebar-bottom">
        <div className="powered-by">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.6">
            <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
          </svg>
          <div className="powered-text">
            <span>ION Platform</span>
            <span className="powered-sub">Global SC Analytics</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
