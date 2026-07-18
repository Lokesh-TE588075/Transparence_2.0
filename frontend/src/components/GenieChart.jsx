import React, { useMemo } from "react";
import {
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";

// ═══════════════════════════════════════════════════════════════
//  Constants
// ═══════════════════════════════════════════════════════════════

const MAX_BAR_POINTS  = 20;   // top-N for category bar charts
const MAX_LINE_POINTS = 50;   // max points for time-series
const MAX_MEASURES    = 3;    // max y-axis series shown at once

// TE brand palette — primary orange first, then accessible accents
const TE_COLORS = ["#F28C00", "#3B82F6", "#10B981", "#8B5CF6", "#EF4444"];

// Column-name patterns that indicate the column is an opaque identifier
// and should NOT be used as a chart dimension.
const ID_SUFFIX_RE = /(_id|_key|_number|_no|_num|_ref|_code|shipment_no|shipment_id)$/i;

// Column-name patterns that strongly suggest a date / time dimension.
const DATE_NAME_RE = /(date|time|month|year|week|day|period|_at|timestamp|quarter)/i;

// ═══════════════════════════════════════════════════════════════
//  Pure helpers  (exported so smoke tests can import them)
// ═══════════════════════════════════════════════════════════════

/**
 * Normalise rows to arrays.
 * Accepts either:
 *   - array rows:  [["v1", "v2"], ...]
 *   - object rows: [{ col1: "v1", col2: "v2" }, ...]
 */
export function normalizeRows(headers, rows) {
  if (!rows || !rows.length) return [];
  return rows.map((row) => {
    if (Array.isArray(row)) return row;
    if (row && typeof row === "object")
      return headers.map((h) => row[h] ?? null);
    return headers.map(() => null);
  });
}

/**
 * Classify each column as dates | categories | numerics.
 * Returns { dates: number[], categories: number[], numerics: number[] }
 * (arrays of column indices).
 *
 * Rules:
 *  - A column is a DATE if its name matches DATE_NAME_RE, OR most sample
 *    values look like ISO dates / quarters.
 *  - A column is NUMERIC if ≥ 80 % of sample values parse as numbers AND
 *    the column name doesn\'t look like an ID.
 *  - A column is a CATEGORY if it\'s neither date nor numeric, its name
 *    doesn\'t look like an ID, and its cardinality is ≤ 2×MAX_BAR_POINTS.
 */
export function inferColumnTypes(headers, normRows) {
  const dates      = [];
  const categories = [];
  const numerics   = [];

  headers.forEach((h, idx) => {
    // Collect up to 15 non-null sample values for this column
    const samples = [];
    for (let i = 0; i < normRows.length && samples.length < 15; i++) {
      const v = normRows[i][idx];
      if (v !== null && v !== undefined && v !== "") samples.push(v);
    }
    if (!samples.length) return; // skip wholly-empty columns

    // ── Date detection ────────────────────────────────────────
    const dateByName = DATE_NAME_RE.test(h);
    const dateByValue = (() => {
      const hits = samples.filter((v) => {
        const s = String(v);
        return (
          /^\d{4}-\d{2}(-\d{2})?$/.test(s) || // YYYY-MM or YYYY-MM-DD
          /^\d{4}Q\d$/.test(s)               || // 2024Q1
          /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)/i.test(s)
        );
      }).length;
      return hits / samples.length >= 0.6;
    })();

    if (dateByName || dateByValue) {
      dates.push(idx);
      return;
    }

    // ── Numeric detection ─────────────────────────────────────
    const numericHits = samples.filter(
      (v) => v !== "" && !isNaN(Number(v))
    ).length;
    if (numericHits / samples.length >= 0.8 && !ID_SUFFIX_RE.test(h)) {
      numerics.push(idx);
      return;
    }

    // ── Category detection ────────────────────────────────────
    if (!ID_SUFFIX_RE.test(h)) {
      const uniq = new Set(samples.map(String)).size;
      if (uniq <= MAX_BAR_POINTS * 2) {
        categories.push(idx);
      }
    }
  });

  return { dates, categories, numerics };
}

/**
 * Given column types, choose a chart configuration.
 * Returns:
 *   { type: \'bar\'|\'line\'|\'none\', xKey?: string, yKeys?: string[], reason?: string }
 *
 * Decision rules:
 *  A. time-series line  — date dim + any numeric measures
 *  B. category bar      — text dim + any numeric measures
 *  C. none (fallback)   — no usable dimension or no numeric measures
 */
export function inferChartConfig(headers, normRows) {
  if (!headers.length || !normRows.length) {
    return { type: "none", reason: "empty data" };
  }

  const { dates, categories, numerics } = inferColumnTypes(headers, normRows);

  if (!numerics.length) {
    return { type: "none", reason: "no numeric measure columns found" };
  }

  // A: time-series line chart
  if (dates.length > 0) {
    return {
      type: "line",
      xKey:  headers[dates[0]],
      yKeys: numerics.slice(0, MAX_MEASURES).map((i) => headers[i]),
    };
  }

  // B / C: category bar chart
  if (categories.length > 0) {
    return {
      type: "bar",
      xKey:  headers[categories[0]],
      yKeys: numerics.slice(0, MAX_MEASURES).map((i) => headers[i]),
    };
  }

  return { type: "none", reason: "no suitable dimension column found" };
}

// ── Internal: convert normRows → Recharts data objects ────────────────────────
function buildChartData(headers, normRows, config) {
  const { type, xKey, yKeys } = config;
  if (type === "none") return [];

  const xIdx  = headers.indexOf(xKey);
  const yIdxs = yKeys.map((k) => headers.indexOf(k));

  // Build plain objects keyed by column name
  let data = normRows.map((row) => {
    const point = { [xKey]: row[xIdx] };
    yKeys.forEach((k, i) => {
      const raw = row[yIdxs[i]];
      point[k] =
        raw !== null && raw !== undefined && raw !== "" && !isNaN(Number(raw))
          ? Number(raw)
          : null;
    });
    return point;
  });

  // Drop rows where every y value is null
  data = data.filter((p) => yKeys.some((k) => p[k] !== null));

  if (type === "line") {
    // Sort ascending by x (lexicographic; works for ISO dates and quarter strings)
    data.sort((a, b) => String(a[xKey] ?? "").localeCompare(String(b[xKey] ?? "")));
    return data.slice(0, MAX_LINE_POINTS);
  }

  if (type === "bar") {
    // Sort descending by the first measure → top-N categories
    const firstY = yKeys[0];
    data.sort((a, b) => (b[firstY] ?? -Infinity) - (a[firstY] ?? -Infinity));
    return data.slice(0, MAX_BAR_POINTS);
  }

  return data;
}

// ── Axis / tooltip formatters ──────────────────────────────────────────────────
const fmtTick = (v) => {
  const s = String(v ?? "");
  return s.length > 14 ? s.slice(0, 12) + "\u2026" : s; // truncate long labels
};

const fmtNumber = (v) => {
  if (typeof v !== "number") return String(v ?? "");
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)     return `${(v / 1_000).toFixed(1)}K`;
  return v % 1 === 0  ? v.toLocaleString() : v.toFixed(2);
};

// ═══════════════════════════════════════════════════════════════
//  GenieChart component
// ═══════════════════════════════════════════════════════════════

/**
 * GenieChart
 *
 * Client-side chart rendered from the query result data returned by the
 * Genie backend.  The Genie viz attachment does NOT expose a chart spec;
 * this component infers a suitable chart type from the data shape.
 *
 * Props:
 *   tableData         { headers: string[], rows: any[][] | object[] }
 *   visualization     { type, query_attachment_id, render_strategy, ... }
 *   rowCount          number | undefined   Total server row count.
 *   title             string | undefined   Optional override title.
 *   computedChartData { data: object[], x_key: string, y_key: string } | null
 *                     Pre-computed chart data from the backend table summarizer (Q3).
 *                     Used as fallback when inferred chart type is "none".
 */
export default function GenieChart({ tableData, visualization, rowCount, title, computedChartData }) {
  const { config, chartData } = useMemo(() => {
    // ── Path A: infer chart from tableData (Genie native viz or inline data) ──
    if (tableData) {
      const { headers = [], rows = [] } = tableData;
      if (headers.length && rows.length) {
        const norm = normalizeRows(headers, rows);
        const cfg  = inferChartConfig(headers, norm);
        if (cfg.type !== "none") {
          const data = buildChartData(headers, norm, cfg);
          if (data.length > 0) {
            return { config: cfg, chartData: data };
          }
        }
      }
    }

    // ── Path B: use pre-computed chart data from backend summarizer (Q3) ──────
    if (
      computedChartData &&
      Array.isArray(computedChartData.data) &&
      computedChartData.data.length > 0 &&
      computedChartData.x_key &&
      computedChartData.y_key
    ) {
      return {
        config: {
          type:  "bar",
          xKey:  computedChartData.x_key,
          yKeys: [computedChartData.y_key],
        },
        chartData: computedChartData.data,
      };
    }

    return { config: { type: "none", reason: "no chartable data" }, chartData: [] };
  }, [tableData, computedChartData]);

  // ── Graceful fallback ──────────────────────────────────────────────────────
  // Render nothing when no chart can be inferred — the table view handles it.
  if (config.type === "none" || !chartData.length) {
    return null;
  }

  const { type, xKey, yKeys } = config;

  const autoTitle =
    title ||
    (type === "bar"  ? `${yKeys[0]} by ${xKey}` : null) ||
    (type === "line" ? `${yKeys[0]} over time`   : null) ||
    "";

  // ── Bar chart ──────────────────────────────────────────────────────────────
  if (type === "bar") {
    return (
      <div className="genie-chart-container">
        {autoTitle && <div className="genie-chart-header">{autoTitle}</div>}
        <div className="genie-chart-body">
          <ResponsiveContainer width="100%" height={260}>
            <BarChart
              data={chartData}
              margin={{ top: 6, right: 16, left: 0, bottom: 48 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#E5E5E5" />
              <XAxis
                dataKey={xKey}
                tick={{ fontSize: 11, fill: "#6B6B6B" }}
                tickFormatter={fmtTick}
                angle={-30}
                textAnchor="end"
                interval={0}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "#6B6B6B" }}
                tickFormatter={fmtNumber}
                width={58}
              />
              <Tooltip
                formatter={(v, name) => [fmtNumber(v), name]}
                contentStyle={{ fontSize: 12, borderRadius: 6 }}
              />
              {yKeys.length > 1 && (
                <Legend wrapperStyle={{ fontSize: 12, paddingTop: 4 }} />
              )}
              {yKeys.map((k, i) => (
                <Bar
                  key={k}
                  dataKey={k}
                  fill={TE_COLORS[i % TE_COLORS.length]}
                  radius={[3, 3, 0, 0]}
                  maxBarSize={52}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
        {rowCount != null && rowCount > MAX_BAR_POINTS && (
          <div className="genie-chart-note">
            Showing top {chartData.length} of {rowCount.toLocaleString()} by {yKeys[0]}.
          </div>
        )}
      </div>
    );
  }

  // ── Line chart ─────────────────────────────────────────────────────────────
  if (type === "line") {
    return (
      <div className="genie-chart-container">
        {autoTitle && <div className="genie-chart-header">{autoTitle}</div>}
        <div className="genie-chart-body">
          <ResponsiveContainer width="100%" height={260}>
            <LineChart
              data={chartData}
              margin={{ top: 6, right: 16, left: 0, bottom: 24 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#E5E5E5" />
              <XAxis
                dataKey={xKey}
                tick={{ fontSize: 11, fill: "#6B6B6B" }}
                tickFormatter={fmtTick}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fontSize: 11, fill: "#6B6B6B" }}
                tickFormatter={fmtNumber}
                width={58}
              />
              <Tooltip
                formatter={(v, name) => [fmtNumber(v), name]}
                contentStyle={{ fontSize: 12, borderRadius: 6 }}
              />
              {yKeys.length > 1 && (
                <Legend wrapperStyle={{ fontSize: 12, paddingTop: 4 }} />
              )}
              {yKeys.map((k, i) => (
                <Line
                  key={k}
                  type="monotone"
                  dataKey={k}
                  stroke={TE_COLORS[i % TE_COLORS.length]}
                  strokeWidth={2}
                  dot={chartData.length <= 20}
                  activeDot={{ r: 4 }}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    );
  }

  return null;
}
