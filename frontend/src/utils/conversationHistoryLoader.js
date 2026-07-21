/**
 * conversationHistoryLoader.js
 *
 * Fetches persisted conversation history from the backend and maps
 * the server response to frontend message objects for rehydration.
 *
 * Security contract:
 * - Never stores message content in localStorage or sessionStorage.
 * - Maps only safe frontend fields; internal identifiers (owner_hash,
 *   genie_conversation_id, generated_sql) are never forwarded.
 * - AbortSignal support allows race-condition cancellation when the user
 *   switches conversations before the previous load completes.
 *
 * H1 — TransparencE Conversation History Persistence
 */

const PAGE_SIZE = 50;

/**
 * Load conversation history from the backend.
 *
 * @param {string} frontendConversationId  - The conversation ID (from frontend state)
 * @param {AbortSignal} [signal]           - Optional AbortController signal
 * @returns {Promise<{messages: Array, total: number, status: string}>}
 *          Resolves with mapped messages.  Resolves with empty messages
 *          for inactive/disabled/not-found conversations (not an error).
 *          Rejects only on network failure (allows caller to decide).
 */
export async function loadConversationHistory(frontendConversationId, signal) {
  if (!frontendConversationId) {
    return { messages: [], total: 0, status: 'no_id' };
  }

  const url = `/api/conversations/${encodeURIComponent(frontendConversationId)}/messages?page=1&page_size=${PAGE_SIZE}`;

  let response;
  try {
    response = await fetch(url, {
      method: 'GET',
      credentials: 'same-origin',
      signal,
    });
  } catch (err) {
    if (err && err.name === 'AbortError') {
      // Caller cancelled the request — return empty gracefully
      return { messages: [], total: 0, status: 'aborted' };
    }
    throw err;
  }

  // Non-2xx responses that are "expected" inactive states should still
  // resolve with empty messages so the UI shows a clean empty state.
  if (!response.ok) {
    if (response.status === 503) {
      // Feature disabled or temporarily unavailable — not a user-facing error
      return { messages: [], total: 0, status: 'unavailable' };
    }
    if (response.status === 401 || response.status === 403) {
      return { messages: [], total: 0, status: 'unauthorized' };
    }
    // Other error: surface to caller
    throw new Error(`History load failed: HTTP ${response.status}`);
  }

  let data;
  try {
    data = await response.json();
  } catch {
    return { messages: [], total: 0, status: 'parse_error' };
  }

  // Backend may return a status field indicating inactive/disabled state
  const backendStatus = data.status;
  if (
    backendStatus === 'inactive' ||
    backendStatus === 'not_found' ||
    backendStatus === 'disabled' ||
    backendStatus === 'error'
  ) {
    return { messages: [], total: 0, status: backendStatus };
  }

  const rawMessages = Array.isArray(data.messages) ? data.messages : [];
  const mapped = rawMessages.map((item, idx) => mapHistoryItem(item, idx));

  return {
    messages: mapped,
    total: data.total_messages || mapped.length,
    status: 'loaded',
    hasMore: data.has_more || false,
  };
}

/**
 * Map a single backend HistoryMessageItem to a frontend message object.
 *
 * Only safe, non-sensitive fields are forwarded.
 * isHistorical=true prevents the chat pipeline from treating these as
 * live messages during the current session.
 *
 * @param {Object} item  - HistoryMessageItem from backend
 * @param {number} idx   - Fallback index for stable keys
 * @returns {Object}     - Frontend message object
 */
function mapHistoryItem(item, idx) {
  return {
    id: `history-${item.id || idx}`,
    role: item.role || 'assistant',
    content: item.content || '',
    status: item.status || 'success',
    isTable: item.is_table || false,
    tableData: item.table_data || null,
    rowCount: typeof item.row_count === 'number' ? item.row_count : 0,
    previewRowCount: typeof item.row_count === 'number' ? item.row_count : 0,
    returnedRowCount: typeof item.row_count === 'number' ? item.row_count : 0,
    totalRowCount: typeof item.row_count === 'number' ? item.row_count : 0,
    suggestedQuestions: item.suggested_questions || null,
    computedChartData: item.computed_chart_data || null,
    computedMetrics: item.computed_metrics || null,
    queryDescription: item.query_description || null,
    hasVisualization: item.has_visualization || false,
    source: item.source || null,
    timestamp: item.timestamp ? new Date(item.timestamp) : new Date(),
    isHistorical: true,  // prevents duplicate re-send on new message
    // Intentionally absent: downloadKey, exportId, exportStatus, exportMode,
    // exportRowCount, executionTime, generatedSql, genieConversationId,
    // genieMessageId — these are not stored in history for security reasons.
  };
}
