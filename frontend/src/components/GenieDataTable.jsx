import React, { useEffect, useState } from "react";

export default function GenieDataTable({
  data,
  rowCount,
  previewRowCount = 0,
  returnedRowCount,
  totalRowCount,
  exportRowCount,
  downloadKey,
  exportId,
  exportStatus,
  exportMode,
  displayRowLimit = 100,
}) {
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState("asc");
  const [currentDownloadKey, setCurrentDownloadKey] = useState(downloadKey || null);
  const [currentExportStatus, setCurrentExportStatus] = useState(exportStatus || null);
  const [currentExportRowCount, setCurrentExportRowCount] = useState(exportRowCount ?? null);
  const [currentExportMode, setCurrentExportMode] = useState(exportMode || null);

  useEffect(() => {
    setCurrentDownloadKey(downloadKey || null);
    setCurrentExportStatus(exportStatus || null);
    setCurrentExportRowCount(exportRowCount ?? null);
    setCurrentExportMode(exportMode || null);
  }, [downloadKey, exportStatus, exportRowCount, exportMode]);

  useEffect(() => {
    if (!exportId || !["queued", "running"].includes(currentExportStatus || "")) {
      return undefined;
    }

    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(`/api/export/status/${exportId}`);
        if (!res.ok) return;
        const payload = await res.json();
        if (cancelled) return;
        setCurrentExportStatus(payload.status || null);
        setCurrentDownloadKey(payload.download_key || null);
        setCurrentExportRowCount(payload.row_count ?? null);
        setCurrentExportMode(payload.mode || currentExportMode);
      } catch (_) {
        // Non-fatal. Keep current status and try again on next interval.
      }
    };

    poll();
    const id = window.setInterval(poll, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [exportId, currentExportStatus, currentExportMode]);

  if (!data) return null;

  const { headers = [], rows = [] } = data;

  if (!headers.length && !rows.length) {
    return <div className="data-table-empty">No data returned.</div>;
  }

  const normalizedRows = rows.map((row) => {
    if (Array.isArray(row)) return row;
    if (row && typeof row === "object") return headers.map((h) => row[h] ?? null);
    return [];
  });

  const sortedRows = [...normalizedRows];
  if (sortCol !== null) {
    const idx = headers.indexOf(sortCol);
    sortedRows.sort((a, b) => {
      const va = a[idx];
      const vb = b[idx];
      if (va == null) return 1;
      if (vb == null) return -1;
      const na = Number(va);
      const nb = Number(vb);
      if (!isNaN(na) && !isNaN(nb)) {
        return sortDir === "asc" ? na - nb : nb - na;
      }
      return sortDir === "asc"
        ? String(va).localeCompare(String(vb))
        : String(vb).localeCompare(String(va));
    });
  }

  const handleHeaderClick = (h) => {
    if (sortCol === h) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(h);
      setSortDir("asc");
    }
  };

  const visibleRows = sortedRows.slice(0, displayRowLimit);
  const displayedCount = previewRowCount || visibleRows.length;
  const returnedCount = returnedRowCount ?? rowCount ?? rows.length;
  const hasReturnedOverflow = returnedCount > displayedCount;
  const cappedAtPreview = displayedCount >= displayRowLimit && returnedCount >= displayedCount;

  const countLabel = hasReturnedOverflow
    ? `Showing first ${displayedCount.toLocaleString()} of ${returnedCount.toLocaleString()} returned rows.`
    : `Showing first ${displayedCount.toLocaleString()} rows.`;

  // Contract 8: per-table export status label — clearly references "this table"
  const exportStatusLabel = (() => {
    if (currentExportStatus === "queued" || currentExportStatus === "running") {
      return "Preparing CSV export for this table…";
    }
    if (currentExportStatus === "failed") {
      return "CSV export failed. Try narrowing the query.";
    }
    if (currentDownloadKey && currentExportRowCount != null) {
      return `CSV export for this table is ready (${currentExportRowCount.toLocaleString()} rows).`;
    }
    if (currentDownloadKey) {
      return "CSV export for this table is ready.";
    }
    return null;
  })();

  // Contract 8: download button label includes row count when available
  const downloadBtnLabel = (() => {
    if (currentExportRowCount != null) {
      return `Download CSV (${currentExportRowCount.toLocaleString()} rows)`;
    }
    return "Download CSV";
  })();

  return (
    <div className="data-table-container">
      <div className="table-header-bar">
        <div className="table-meta-block">
          <span className="table-count">{countLabel}</span>
          {totalRowCount != null && totalRowCount > returnedCount && (
            <span className="table-subcount">
              Total matching rows known: {totalRowCount.toLocaleString()}.
            </span>
          )}
          {exportStatusLabel && (
            <span className={`table-export-status ${currentExportStatus || "idle"}`}>
              {exportStatusLabel}
            </span>
          )}
          {currentExportMode === "async_full_query" && (
            <span className="table-subcount">Export capped at 100,000 rows.</span>
          )}
        </div>
        {currentDownloadKey ? (
          <a
            className="download-link"
            href={`/api/download/${currentDownloadKey}`}
            target="_blank"
            rel="noreferrer"
            title={downloadBtnLabel}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
            </svg>
            {downloadBtnLabel}
          </a>
        ) : ["queued", "running"].includes(currentExportStatus || "") ? (
          <button className="download-link disabled" disabled>
            <span className="table-spinner" />
            Preparing CSV…
          </button>
        ) : null}
      </div>

      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {headers.map((h) => (
                <th key={h} onClick={() => handleHeaderClick(h)}>
                  {h}
                  {sortCol === h && (
                    <span className="sort-arrow">
                      {sortDir === "asc" ? " \u25b2" : " \u25bc"}
                    </span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{cell != null ? String(cell) : "\u2014"}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {cappedAtPreview && (
        <div className="table-footer">
          Showing first {displayedCount.toLocaleString()} rows.
        </div>
      )}
    </div>
  );
}
