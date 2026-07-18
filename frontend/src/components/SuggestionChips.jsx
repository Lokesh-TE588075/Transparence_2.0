import React, { useState } from "react";

/**
 * SuggestionChips
 *
 * Renders suggested_questions from the Genie backend as clickable follow-up
 * prompt chips displayed below the assistant response.
 *
 * Behaviour:
 *   - Returns null when suggestions is empty or missing.
 *   - Once any chip is clicked, all chips are disabled to prevent duplicate sends.
 *   - The clicked chip is visually highlighted; the rest dim.
 *   - Calls onSelect(text) to send the question (same as typing it in the input).
 *
 * Props:
 *   suggestions  string[]            Genie suggested_questions array.
 *   onSelect     (text: string)=>void Callback to send the selected question.
 */
export default function SuggestionChips({ suggestions, onSelect }) {
  const [clickedIdx, setClickedIdx] = useState(null);

  if (!suggestions || suggestions.length === 0) return null;

  const handleClick = (text, idx) => {
    if (clickedIdx !== null) return; // prevent double-send
    setClickedIdx(idx);
    onSelect(text);
  };

  return (
    <div
      className="suggestion-chips"
      role="list"
      aria-label="Suggested follow-up questions"
    >
      {suggestions.map((q, i) => (
        <button
          key={i}
          role="listitem"
          className={[
            "suggestion-chip",
            clickedIdx === i ? "selected" : "",
            clickedIdx !== null && clickedIdx !== i ? "dimmed" : "",
          ]
            .filter(Boolean)
            .join(" ")}
          onClick={() => handleClick(q, i)}
          disabled={clickedIdx !== null}
          title={q}
        >
          {q}
        </button>
      ))}
    </div>
  );
}
