import React from "react";

export default function Header({ onToggleSidebar, onHelp, onFeedback }) {
  return (
    <header className="header">
      <div className="header-left">
        <button className="icon-btn" onClick={onToggleSidebar} title="Toggle sidebar" aria-label="Toggle sidebar">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M3 12h18M3 6h18M3 18h18"/>
          </svg>
        </button>
        <div className="te-logo" title="TE Connectivity">
          <svg width="180" height="54" viewBox="0 0 120 38" xmlns="http://www.w3.org/2000/svg">
            {/* TE connector icon - 3 rounded bars */}
            <rect x="2" y="6" width="28" height="8" rx="4" ry="4" fill="#F28C00"/>
            <rect x="10" y="17" width="20" height="8" rx="4" ry="4" fill="#F28C00"/>
            <rect x="2" y="28" width="28" height="8" rx="4" ry="4" fill="#F28C00"/>
            {/* TE text */}
            <text x="38" y="28" fontFamily="Arial, sans-serif" fontSize="22" fontWeight="bold" fill="#F28C00">TE</text>
            {/* connectivity text */}
            <text x="38" y="36" fontFamily="Arial, sans-serif" fontSize="7" fill="#666" letterSpacing="0.5">connectivity</text>
          </svg>
        </div>
      </div>
      <div className="header-center">
        <h1 className="brand-title">
          <span className="brand-accent">T</span>ransparenc<span className="brand-accent">E</span>
        </h1>
        <span className="brand-subtitle">Shipment Intelligence</span>
      </div>
      <div className="header-right">
        <button className="header-action-btn" onClick={onHelp}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r="0.5" fill="currentColor"/>
          </svg>
          Help
        </button>
        <button className="header-action-btn" onClick={onFeedback}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
          </svg>
          Feedback
        </button>
      </div>
    </header>
  );
}
