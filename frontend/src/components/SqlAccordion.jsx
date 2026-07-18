import React, { useState } from "react";

/**
 * SqlAccordion
 *
 * Renders generated_sql in a collapsed accordion for debugging and transparency.
 * Only mounts when the sql prop is a non-empty string.
 *
 * Behaviour:
 *   - Collapsed by default.
 *   - Toggle arrow (\u25b6/\u25bc) reflects open/close state.
 *   - Copy-to-clipboard button resets to "Copy" after 2 s.
 *   - Clipboard write failure is silent (graceful degradation in HTTP envs).
 *
 * Props:
 *   sql  string | null | undefined  The SQL string produced by Genie.
 */
export default function SqlAccordion({ sql }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  if (!sql) return null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(sql);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      // Clipboard API may be unavailable over HTTP or in restricted contexts.
      console.warn("SqlAccordion: clipboard write failed", err);
    }
  };

  return (
    <div className="sql-accordion">
      <button
        className="sql-accordion-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="sql-accordion-arrow" aria-hidden="true">
          {open ? "\u25bc" : "\u25b6"}
        </span>
        View generated SQL
      </button>

      {open && (
        <div className="sql-accordion-body">
          <button
            className={`sql-copy-btn${copied ? " copied" : ""}`}
            onClick={handleCopy}
            title="Copy SQL to clipboard"
          >
            {copied ? "\u2713 Copied" : "Copy"}
          </button>
          <pre className="sql-code">{sql}</pre>
        </div>
      )}
    </div>
  );
}
