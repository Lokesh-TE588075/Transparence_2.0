import React, { useState } from "react";

const FAQ = [
  { q: "What data can I query?", a: "TransparencE has access to shipment records including tracking numbers, origins, destinations, transport modes, costs, delays, and status. Data covers shipments from October 2025 onwards." },
  { q: "How do I search for a specific shipment?", a: "Simply type the shipment number, e.g., \"Show details for 7004734952\" or \"What is the status of shipment 7004735587?\"" },
  { q: "Can I export results?", a: "Yes! When results are returned as a table, you\u2019ll see a \u2018Download CSV\u2019 button that exports the full result set." },
  { q: "What types of questions work best?", a: "Be specific: mention shipment numbers, date ranges, business units, countries, or metrics. Examples: \"Total revenue for E-Mobility in June\", \"Delayed shipments to Germany\", \"Top 10 part numbers by weight\"." },
  { q: "Why did my query fail?", a: "This can happen if the question is ambiguous. Try being more specific about what columns or filters you need. If the issue persists, use the Feedback button to report it." },
  { q: "What transport modes are tracked?", a: "Air transport and Ocean transport are the two primary modes in the system." },
];

export default function HelpModal({ onClose }) {
  const [expandedIdx, setExpandedIdx] = useState(null);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content help-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Help & Guide</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">&times;</button>
        </div>
        <div className="modal-body">
          <div className="help-section">
            <h3>Quick Tips</h3>
            <div className="tips-grid">
              <div className="tip-card">
                <span className="tip-icon">🎯</span>
                <strong>Be specific</strong>
                <p>Include shipment numbers, dates, or business units for precise results.</p>
              </div>
              <div className="tip-card">
                <span className="tip-icon">📊</span>
                <strong>Ask for metrics</strong>
                <p>Revenue, weight, delay days, shipment counts — all available.</p>
              </div>
              <div className="tip-card">
                <span className="tip-icon">🔍</span>
                <strong>Filter freely</strong>
                <p>By country, transport mode, status, date range, or business unit.</p>
              </div>
            </div>
          </div>
          <div className="help-section">
            <h3>Frequently Asked Questions</h3>
            <div className="faq-list">
              {FAQ.map((item, i) => (
                <div key={i} className={`faq-item ${expandedIdx === i ? "expanded" : ""}`}>
                  <button className="faq-question" onClick={() => setExpandedIdx(expandedIdx === i ? null : i)}>
                    {item.q}
                    <span className="faq-chevron">{expandedIdx === i ? "\u25B2" : "\u25BC"}</span>
                  </button>
                  {expandedIdx === i && <div className="faq-answer">{item.a}</div>}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
