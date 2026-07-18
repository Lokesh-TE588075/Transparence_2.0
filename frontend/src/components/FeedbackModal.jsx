import React, { useState } from "react";

export default function FeedbackModal({ onClose, conversationId }) {
  const [rating, setRating] = useState(0);
  const [category, setCategory] = useState("");
  const [comment, setComment] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!rating) return;
    setSubmitting(true);
    try {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rating,
          category,
          comment,
          conversation_id: conversationId,
          feedback_type: "general",
        }),
      });
      setSubmitted(true);
    } catch (err) {
      console.error("Feedback submission error:", err);
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <div className="modal-overlay" onClick={onClose}>
        <div className="modal-content feedback-modal" onClick={e => e.stopPropagation()}>
          <div className="feedback-success">
            <div className="success-icon">✓</div>
            <h3>Thank you!</h3>
            <p>Your feedback helps us improve TransparencE.</p>
            <button className="btn-primary" onClick={onClose}>Done</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content feedback-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Share Feedback</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">&times;</button>
        </div>
        <form className="modal-body" onSubmit={handleSubmit}>
          <div className="form-group">
            <label>How would you rate your experience?</label>
            <div className="star-rating">
              {[1, 2, 3, 4, 5].map(n => (
                <button key={n} type="button" className={`star ${rating >= n ? "filled" : ""}`} onClick={() => setRating(n)}>
                  ★
                </button>
              ))}
            </div>
          </div>
          <div className="form-group">
            <label>Category</label>
            <select value={category} onChange={e => setCategory(e.target.value)}>
              <option value="">Select a category...</option>
              <option value="accuracy">Query Accuracy</option>
              <option value="speed">Response Speed</option>
              <option value="ui">User Interface</option>
              <option value="feature">Feature Request</option>
              <option value="bug">Bug Report</option>
              <option value="other">Other</option>
            </select>
          </div>
          <div className="form-group">
            <label>Comments (optional)</label>
            <textarea
              value={comment}
              onChange={e => setComment(e.target.value)}
              placeholder="Tell us what went well or what we can improve..."
              rows={4}
            />
          </div>
          <div className="form-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary" disabled={!rating || submitting}>
              {submitting ? "Submitting..." : "Submit Feedback"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
