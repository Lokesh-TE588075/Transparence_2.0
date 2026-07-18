import React, { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import GenieDataTable from "./GenieDataTable";
import SuggestionChips from "./SuggestionChips";
import SqlAccordion from "./SqlAccordion";
import GenieChart from "./GenieChart";

const SUGGESTIONS = [
  { icon: "📦", text: "Show details for shipment 7004734952" },
  { icon: "⏱️", text: "Which shipments are delayed more than 7 days?" },
  { icon: "💰", text: "Total revenue by business unit this month" },
  { icon: "✈️", text: "Compare air vs ocean transport volumes" },
];

export default function ChatWindow({ conversation, isLoading, onSend, onFeedback }) {
  const [input, setInput] = useState("");
  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [conversation.messages, isLoading]);

  useEffect(() => {
    if (!isLoading && textareaRef.current) textareaRef.current.focus();
  }, [isLoading]);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 150) + "px";
    }
  }, [input]);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (input.trim() && !isLoading) {
      onSend(input.trim());
      setInput("");
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const isEmpty = conversation.messages.length === 0;

  return (
    <div className="chat-window">
      <div className="messages-area">
        {isEmpty ? (
          <div className="welcome-screen">
            <div className="welcome-icon">
              <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
                <rect x="4" y="8" width="22" height="6" rx="3" fill="#F28C00" opacity="0.8"/>
                <rect x="10" y="18" width="16" height="6" rx="3" fill="#F28C00" opacity="0.6"/>
                <rect x="4" y="28" width="22" height="6" rx="3" fill="#F28C00" opacity="0.4"/>
                <text x="30" y="28" fontFamily="Arial" fontSize="16" fontWeight="bold" fill="#F28C00">TE</text>
              </svg>
            </div>
            <h2 className="welcome-title">What can I help you find?</h2>
            <p className="welcome-sub">Ask about shipments, delays, revenue, transport modes, and more.</p>
            <div className="suggestions-grid">
              {SUGGESTIONS.map((s, i) => (
                <button key={i} className="suggestion-card" onClick={() => onSend(s.text)}>
                  <span className="suggestion-icon">{s.icon}</span>
                  <span className="suggestion-text">{s.text}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            {conversation.messages.map((msg) => (
              <div key={msg.id} className={`message ${msg.role}`}>
                <div className="msg-avatar-col">
                  {msg.role === "assistant" ? (
                    <div className="avatar bot">
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                        <rect x="2" y="5" width="14" height="4" rx="2" fill="white"/>
                        <rect x="6" y="11" width="10" height="4" rx="2" fill="white"/>
                        <rect x="2" y="17" width="14" height="4" rx="2" fill="white" opacity="0.8"/>
                      </svg>
                    </div>
                  ) : (
                    <div className="avatar user">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
                        <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>
                      </svg>
                    </div>
                  )}
                </div>
                <div className="msg-content-col">
                  <div className="msg-sender">{msg.role === "assistant" ? "TransparencE" : "You"}</div>
                  <div className={`msg-bubble ${msg.status === "error" ? "error" : ""}`}>
                    <ReactMarkdown>{msg.content}</ReactMarkdown>
                    {/* Q2: query context line — short label for what Genie executed */}
                    {msg.queryDescription && (
                      <div className="query-context-line">
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{marginRight:4,flexShrink:0}}>
                          <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
                        </svg>
                        {msg.queryDescription}
                      </div>
                    )}
                    {(msg.hasVisualization || msg.computedChartData) && (
                      <GenieChart
                        tableData={msg.tableData}
                        visualization={msg.visualization}
                        rowCount={msg.rowCount}
                        computedChartData={msg.computedChartData}
                      />
                    )}
                    {msg.isTable && msg.tableData && (
                      <GenieDataTable
                        data={msg.tableData}
                        rowCount={msg.rowCount}
                        previewRowCount={msg.previewRowCount}
                        returnedRowCount={msg.returnedRowCount}
                        totalRowCount={msg.totalRowCount}
                        exportRowCount={msg.exportRowCount}
                        downloadKey={msg.downloadKey}
                        exportId={msg.exportId}
                        exportStatus={msg.exportStatus}
                        exportMode={msg.exportMode}
                        displayRowLimit={msg.displayRowLimit || 100}
                      />
                    )}
                    {!msg.isTable && msg.downloadKey && (
                      <a className="download-link" href={`/api/download/${msg.downloadKey}`} target="_blank" rel="noreferrer">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
                        {msg.exportRowCount == null
                          ? "Download CSV"
                          : `Download CSV (${msg.exportRowCount.toLocaleString()} rows)`}
                      </a>
                    )}
                  </div>
                  {msg.role === "assistant" && (
                    <>
                      <SqlAccordion sql={msg.generatedSql} />
                      <SuggestionChips
                        suggestions={msg.suggestedQuestions}
                        onSelect={onSend}
                      />
                    </>
                  )}
                  {msg.role === "assistant" && msg.status !== "error" && (
                    <div className="msg-actions">
                      <button className={`action-btn ${msg.feedback === 5 ? "active positive" : ""}`} onClick={() => onFeedback(msg.id, 5)} title="Helpful">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill={msg.feedback === 5 ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>
                      </button>
                      <button className={`action-btn ${msg.feedback === 1 ? "active negative" : ""}`} onClick={() => onFeedback(msg.id, 1)} title="Not helpful">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill={msg.feedback === 1 ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg>
                      </button>
                      {msg.executionTime && <span className="exec-time">{(msg.executionTime / 1000).toFixed(1)}s</span>}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {isLoading && (
              <div className="message assistant">
                <div className="msg-avatar-col">
                  <div className="avatar bot">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                      <rect x="2" y="5" width="14" height="4" rx="2" fill="white"/>
                      <rect x="6" y="11" width="10" height="4" rx="2" fill="white"/>
                      <rect x="2" y="17" width="14" height="4" rx="2" fill="white" opacity="0.8"/>
                    </svg>
                  </div>
                </div>
                <div className="msg-content-col">
                  <div className="msg-sender">TransparencE</div>
                  <div className="msg-bubble loading">
                    <div className="typing-indicator"><span/><span/><span/></div>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="input-wrapper">
        <form className="input-form" onSubmit={handleSubmit}>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about shipments, delays, revenue..."
            disabled={isLoading}
            rows={1}
          />
          <button type="submit" className="send-btn" disabled={isLoading || !input.trim()} aria-label="Send message">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/>
            </svg>
          </button>
        </form>
        <p className="input-disclaimer">TransparencE queries live shipment data. Always verify critical decisions.</p>
      </div>
    </div>
  );
}

function DataTable({ data, rowCount, downloadKey }) {
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState("asc");
  const { headers, rows } = data;

  const sortedRows = [...rows];
  if (sortCol !== null) {
    const idx = headers.indexOf(sortCol);
    sortedRows.sort((a, b) => {
      const va = a[idx], vb = b[idx];
      if (va == null) return 1; if (vb == null) return -1;
      const na = Number(va), nb = Number(vb);
      if (!isNaN(na) && !isNaN(nb)) return sortDir === "asc" ? na - nb : nb - na;
      return sortDir === "asc" ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
  }

  return (
    <div className="data-table-container">
      <div className="table-header-bar">
        <span className="table-count">{rowCount?.toLocaleString()} row{rowCount !== 1 ? "s" : ""}</span>
        {downloadKey && (
          <a className="download-link small" href={`/api/download/${downloadKey}`} target="_blank" rel="noreferrer">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
            Export CSV
          </a>
        )}
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {headers.map(h => (
                <th key={h} onClick={() => { setSortCol(h); setSortDir(sortCol === h && sortDir === "asc" ? "desc" : "asc"); }}>
                  {h}
                  {sortCol === h && <span className="sort-arrow">{sortDir === "asc" ? " \u25B2" : " \u25BC"}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows.slice(0, 50).map((row, i) => (
              <tr key={i}>{row.map((cell, j) => <td key={j}>{cell != null ? String(cell) : "\u2014"}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > 50 && <div className="table-footer">Showing 50 of {rows.length.toLocaleString()} rows. Export CSV for full data.</div>}
    </div>
  );
}
