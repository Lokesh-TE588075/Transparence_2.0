"""Databricks Genie API client.

Provides a clean, typed interface for all Genie REST API interactions.
This is the *only* module that talks to the Genie REST API. Nothing else
in the app should call Genie endpoints directly.

Used by:
    app/services/genie_pipeline.py  (Phase G2 — not yet built)

Authentication:
    Uses the Databricks SDK default credential chain — the same pattern as
    LLMService and SQLService. Tokens are never hardcoded, printed, or logged.
    In Databricks Apps, the runtime injects service principal credentials that
    the SDK picks up automatically.

Genie REST endpoints consumed:
    GET  /api/2.0/genie/spaces
    POST /api/2.0/genie/spaces/{space_id}/start-conversation
    POST /api/2.0/genie/spaces/{space_id}/conversations/{cid}/messages
    GET  /api/2.0/genie/spaces/{space_id}/conversations/{cid}/messages/{mid}
    GET  /api/2.0/genie/spaces/{space_id}/conversations/{cid}/messages/{mid}/query-result
    GET  /api/2.0/sql/statements/{statement_id}   (Statement Execution API for row data)

Viz limitation (by design):
    The Genie ``viz`` attachment contains only ``query_attachment_id`` — a
    reference pointer. Genie does NOT expose a chart spec (Vega-Lite, Plotly,
    image, etc.) via API. Charts must be re-rendered client-side from the query
    result. This client faithfully captures what the API returns.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Terminal and in-progress status sets
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "COMPLETED_WITH_ERROR",
    "COMPLETED_WITH_NO_RESULTS",
})

_IN_PROGRESS_STATUSES = frozenset({
    "SUBMITTED",
    "PENDING_WAREHOUSE",
    "FETCHING_METADATA",
    "ASKING_AI",
    "EXECUTING_QUERY",
    "RUNNING_QUERY",
    "FILTERING_CONTEXT",
})

DEFAULT_POLL_INTERVAL_SECONDS: float = 2.0

# =============================================================================
# EXCEPTIONS
# =============================================================================


class GenieClientError(RuntimeError):
    """Raised for Genie API errors (HTTP errors, unexpected responses)."""


class GenieTimeoutError(GenieClientError):
    """Raised when a Genie message does not reach a terminal state in time."""


class GenieExecutionError(GenieClientError):
    """Raised when Genie returns FAILED or CANCELLED status."""


# =============================================================================
# DATA MODELS
# =============================================================================


@dataclass
class GenieSpace:
    """A Genie Space (agent) available in the workspace."""
    space_id: str
    title: str
    warehouse_id: Optional[str] = None


@dataclass
class GenieQueryAttachment:
    """A SQL query attachment produced by Genie."""
    attachment_id: str
    sql: str
    description: str
    statement_id: str
    row_count: Optional[int] = None
    thoughts: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class GenieTextAttachment:
    """A natural-language text attachment produced by Genie (markdown)."""
    content: str
    attachment_id: Optional[str] = None


@dataclass
class GenieVizAttachment:
    """A visualization attachment produced by Genie.

    IMPORTANT: The Genie API does NOT return a chart spec (Vega-Lite, Plotly,
    image, or any other format). This attachment contains only a reference
    (``query_attachment_id``) pointing back to the associated query attachment.
    Charts must be re-rendered client-side from the query result data.
    """
    attachment_id: str
    query_attachment_id: str  # reference only — no chart spec available


@dataclass
class GenieSuggestedQuestions:
    """Follow-up questions suggested by Genie after an answer."""
    attachment_id: str
    questions: List[str] = field(default_factory=list)


@dataclass
class GenieQueryResult:
    """Parsed result of executing a Genie-generated SQL query."""
    statement_id: str
    status: str
    columns: List[Dict[str, Any]] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    row_count: int = 0
    total_row_count: Optional[int] = None
    format: str = "UNKNOWN"
    error: Optional[str] = None


@dataclass
class GenieMessage:
    """Fully parsed Genie message including all attachment types."""
    id: str
    space_id: str
    conversation_id: str
    status: str
    content: str
    message_id: str
    user_id: Optional[int] = None
    created_timestamp: Optional[int] = None
    last_updated_timestamp: Optional[int] = None
    raw_attachments: List[Dict[str, Any]] = field(default_factory=list)
    raw_query_result: Optional[Dict[str, Any]] = None
    # Parsed attachment collections
    text_attachments: List[GenieTextAttachment] = field(default_factory=list)
    query_attachments: List[GenieQueryAttachment] = field(default_factory=list)
    viz_attachments: List[GenieVizAttachment] = field(default_factory=list)
    suggested_questions: List[str] = field(default_factory=list)


# =============================================================================
# GENIE CLIENT
# =============================================================================


class GenieClient:
    """REST client for the Databricks Genie API.

    Authentication uses the Databricks SDK default credential chain:
    - In Databricks Apps: service principal credentials injected at runtime.
    - In local development: CLI auth or DATABRICKS_TOKEN env variable.
    - No secrets are ever hardcoded, logged, or printed.

    Usage::

        client = GenieClient()
        spaces = client.list_spaces()
        space  = client.find_space_by_name("TransparencE Shipment Intelligence")
        resp   = client.start_conversation(space.space_id, "shipments from US via air")
        msg    = client.wait_for_message_completion(
            space.space_id, resp["conversation_id"], resp["message_id"]
        )
        text   = client.extract_text_attachments(msg)
        sql    = client.extract_generated_sql(msg)
    """

    # Default poll interval between status checks
    DEFAULT_POLL_INTERVAL_SECONDS: float = 2.0
    # Genie API base path
    _GENIE_BASE = "/api/2.0/genie"
    # Statement Execution API base path
    _STMT_BASE = "/api/2.0/sql/statements"

    def __init__(
        self,
        host: Optional[str] = None,
        timeout_seconds: int = 30,
        user_token: Optional[str] = None,
    ):
        """Initialise the Genie client.

        Args:
            host: Workspace URL (e.g. ``https://te-ss-coe-dev.cloud.databricks.com``).
                  Falls back to ``DATABRICKS_HOST`` env var / settings.
            timeout_seconds: HTTP request timeout (not the polling timeout).
            user_token: Optional OAuth token to use for Genie API calls.  When
                provided (e.g. from the ``X-Forwarded-Access-Token`` header in
                Databricks Apps), this takes precedence over the SDK credential
                chain so that Genie calls run under the *user's* permissions
                rather than the app service-principal's.  This is the correct
                pattern for user-delegated access in Databricks Apps.
        """
        # Avoid circular import — settings is loaded lazily
        if host:
            self._host = host.rstrip("/")
            if not self._host.startswith("http"):
                self._host = f"https://{self._host}"
        else:
            _host = os.getenv(
                "DATABRICKS_HOST",
                os.getenv("DB_HOST", "https://te-ss-coe-dev.cloud.databricks.com"),
            )
            self._host = _host.rstrip("/")
            if not self._host.startswith("http"):
                self._host = f"https://{self._host}"

        self._http_timeout  = timeout_seconds
        self._user_token    = user_token or ""   # user-delegated token (highest priority)

        # SDK client — used exclusively for credential refresh
        try:
            from databricks.sdk import WorkspaceClient
            self._ws = WorkspaceClient()
        except Exception as e:
            logger.warning(
                "Databricks SDK WorkspaceClient could not be initialised: %s. "
                "Will fall back to DATABRICKS_TOKEN env var.",
                str(e)[:100],
            )
            self._ws = None

    # -------------------------------------------------------------------------
    # AUTH (internal)
    # -------------------------------------------------------------------------

    def _get_headers(self) -> Dict[str, str]:
        """Build auth headers using SDK credential chain.

        Token is obtained in order:
        1. ``DATABRICKS_TOKEN`` env var (explicit PAT or CI token).
        2. SDK ``WorkspaceClient.config.authenticate()`` — covers OAuth,
           service principal, CLI profile, etc.

        Raises:
            GenieClientError: If no token can be obtained.

        Note:
            The token value is NEVER logged, printed, or included in
            error messages — only its presence is checked.
        """
        # Priority 0: explicit user-delegated token (e.g. X-Forwarded-Access-Token)
        token = self._user_token
        if not token:
            token = os.getenv("DATABRICKS_TOKEN", "")
        if not token:
            # Try settings import (may not be available in test context)
            try:
                from app.config import settings as _settings
                token = _settings.DATABRICKS_TOKEN
            except Exception:
                pass

        if not token and self._ws is not None:
            try:
                auth_headers = self._ws.config.authenticate()
                for k, v in auth_headers.items():
                    if k.lower() == "authorization":
                        token = v.replace("Bearer ", "")
                        break
            except Exception as e:
                raise GenieClientError(
                    f"SDK credential refresh failed: {str(e)[:150]}"
                ) from e

        if not token:
            raise GenieClientError(
                "No authentication token available. "
                "Set DATABRICKS_TOKEN or ensure the SDK credential chain is configured."
            )

        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------------------------
    # HTTP (internal)
    # -------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Make an authenticated REST request and return the parsed JSON body.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path, e.g. ``/api/2.0/genie/spaces``.
            json: Request body (for POST).
            params: URL query parameters.

        Returns:
            Parsed JSON response body.

        Raises:
            GenieClientError: On HTTP 4xx/5xx or JSON parse failure.
        """
        url = f"{self._host}{path}"
        headers = self._get_headers()

        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                json=json,
                params=params,
                timeout=self._http_timeout,
            )
        except requests.exceptions.Timeout as e:
            raise GenieClientError(
                f"HTTP request timed out after {self._http_timeout}s: {path}"
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise GenieClientError(
                f"HTTP connection error to {self._host}: {str(e)[:150]}"
            ) from e

        if resp.status_code >= 400:
            raise GenieClientError(
                f"Genie API returned HTTP {resp.status_code} for {method} {path}: "
                f"{resp.text[:300]}"
            )

        try:
            return resp.json()
        except ValueError as e:
            raise GenieClientError(
                f"Genie API response is not valid JSON: {resp.text[:200]}"
            ) from e

    # -------------------------------------------------------------------------
    # PUBLIC API — Space discovery
    # -------------------------------------------------------------------------

    def list_spaces(self) -> List[GenieSpace]:
        """List all Genie Spaces the calling identity has access to.

        Returns:
            List of GenieSpace objects.

        Raises:
            GenieClientError: On API failure.
        """
        data = self._request("GET", f"{self._GENIE_BASE}/spaces")
        spaces = data.get("spaces", [])
        return [
            GenieSpace(
                space_id=s.get("space_id", ""),
                title=s.get("title", ""),
                warehouse_id=s.get("warehouse_id"),
            )
            for s in spaces
        ]

    def find_space_by_name(self, name: str) -> Optional[GenieSpace]:
        """Find a Genie Space by its display title (case-insensitive).

        Args:
            name: Exact or case-insensitive display name of the space.

        Returns:
            The first matching GenieSpace, or None if not found.
        """
        name_lower = name.lower()
        for space in self.list_spaces():
            if space.title.lower() == name_lower:
                return space
        return None

    # -------------------------------------------------------------------------
    # PUBLIC API — Conversation management
    # -------------------------------------------------------------------------

    def start_conversation(
        self,
        space_id: str,
        message: str,
    ) -> Dict[str, str]:
        """Start a new conversation in a Genie Space.

        Args:
            space_id: ID of the Genie Space to converse in.
            message: First user message.

        Returns:
            Dict with ``conversation_id`` and ``message_id`` keys.

        Raises:
            GenieClientError: On API failure.
        """
        data = self._request(
            "POST",
            f"{self._GENIE_BASE}/spaces/{space_id}/start-conversation",
            json={"content": message},
        )
        return {
            "conversation_id": data.get("conversation_id", ""),
            "message_id": (
                data.get("message_id")
                or data.get("message", {}).get("id", "")
            ),
        }

    def send_message(
        self,
        space_id: str,
        conversation_id: str,
        message: str,
    ) -> Dict[str, str]:
        """Send a follow-up message in an existing Genie conversation.

        Args:
            space_id: ID of the Genie Space.
            conversation_id: Existing conversation ID (from start_conversation).
            message: Follow-up user message.

        Returns:
            Dict with ``message_id`` key.

        Raises:
            GenieClientError: On API failure.
        """
        path = (
            f"{self._GENIE_BASE}/spaces/{space_id}"
            f"/conversations/{conversation_id}/messages"
        )
        data = self._request("POST", path, json={"content": message})
        msg_id = (
            data.get("message_id")
            or data.get("id")
            or data.get("message", {}).get("id", "")
        )
        return {"message_id": msg_id}

    # -------------------------------------------------------------------------
    # PUBLIC API — Message retrieval and polling
    # -------------------------------------------------------------------------

    def get_message(
        self,
        space_id: str,
        conversation_id: str,
        message_id: str,
    ) -> GenieMessage:
        """Retrieve the current state of a Genie message.

        Args:
            space_id: ID of the Genie Space.
            conversation_id: Conversation ID.
            message_id: Message ID to retrieve.

        Returns:
            GenieMessage with status and all parsed attachments.

        Raises:
            GenieClientError: On API failure.
        """
        path = (
            f"{self._GENIE_BASE}/spaces/{space_id}"
            f"/conversations/{conversation_id}/messages/{message_id}"
        )
        raw = self._request("GET", path)
        return self._parse_message(raw)

    def wait_for_message_completion(
        self,
        space_id: str,
        conversation_id: str,
        message_id: str,
        timeout_seconds: int = 120,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        _diagnostic_poll_counter: Optional[List[int]] = None,
    ) -> GenieMessage:
        """Poll a Genie message until it reaches a terminal status.

        Polls ``get_message`` at ``poll_interval_seconds`` intervals until:
        - Status is in ``_TERMINAL_STATUSES`` → returns the final message.
        - ``timeout_seconds`` is exceeded → raises ``GenieTimeoutError``.
        - Status is ``FAILED`` or ``CANCELLED`` → raises ``GenieExecutionError``.

        Args:
            space_id: ID of the Genie Space.
            conversation_id: Conversation ID.
            message_id: Message ID to poll.
            timeout_seconds: Maximum total wait time.
            poll_interval_seconds: Sleep duration between polls.
            _diagnostic_poll_counter: Optional single-element list.  When
                provided, the poll attempt count is written to index 0 on each
                iteration.  Purely diagnostic — has no effect on behaviour.

        Returns:
            The completed GenieMessage.

        Raises:
            GenieTimeoutError: If the message does not complete within the timeout.
            GenieExecutionError: If the message reaches FAILED or CANCELLED status.
            GenieClientError: On unexpected API failure.
        """
        deadline = time.monotonic() + timeout_seconds
        attempt = 0

        while True:
            attempt += 1
            # Diagnostic: update counter in-place so caller sees final attempt count
            if _diagnostic_poll_counter is not None:
                _diagnostic_poll_counter[:] = [attempt]

            msg = self.get_message(space_id, conversation_id, message_id)
            status = msg.status

            logger.debug(
                "Genie poll [%d] space=%s conv=%s msg=%s status=%s",
                attempt,
                space_id,
                conversation_id,
                message_id,
                status,
            )

            if status in {"FAILED", "CANCELLED"}:
                raise GenieExecutionError(
                    f"Genie message {message_id} reached terminal status {status}."
                )

            if status in _TERMINAL_STATUSES:
                return msg

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GenieTimeoutError(
                    f"Genie message {message_id} did not complete within "
                    f"{timeout_seconds}s. Last status: {status}."
                )

            sleep_time = min(poll_interval_seconds, remaining)
            time.sleep(sleep_time)

    # -------------------------------------------------------------------------
    # PUBLIC API — Query results
    # -------------------------------------------------------------------------

    def fetch_query_result(
        self,
        space_id: str,
        conversation_id: str,
        message_id: str,
        statement_id: Optional[str] = None,
        fetch_rows: bool = True,
        row_limit: int = 500,
    ) -> GenieQueryResult:
        """Fetch the query result for a completed Genie message.

        Step 1: Calls the Genie ``/query-result`` endpoint to obtain the
        schema (column names and types) and the ``statement_id``.

        Step 2 (when ``fetch_rows=True``): Calls the Databricks Statement
        Execution API ``GET /api/2.0/sql/statements/{statement_id}`` to
        retrieve actual row data.

        Args:
            space_id: ID of the Genie Space.
            conversation_id: Conversation ID.
            message_id: Message ID whose result to fetch.
            statement_id: If already known (from a query attachment), skip
                Step 1 and go straight to the Statement Execution API.
            fetch_rows: If True (default), also fetch row data.
            row_limit: Maximum rows to return.

        Returns:
            GenieQueryResult with schema and (optionally) row data.

        Raises:
            GenieClientError: On API failure.
        """
        # Step 1: Get schema via Genie query-result endpoint
        if statement_id is None:
            path = (
                f"{self._GENIE_BASE}/spaces/{space_id}"
                f"/conversations/{conversation_id}/messages/{message_id}/query-result"
            )
            raw = self._request("GET", path)
            result = self.parse_query_result_response(raw)
            statement_id = result.statement_id
        else:
            # statement_id provided — build a minimal result to enrich
            result = GenieQueryResult(
                statement_id=statement_id,
                status="UNKNOWN",
            )

        # Step 2: Fetch rows from Statement Execution API
        if fetch_rows and statement_id:
            try:
                stmt_path = f"{self._STMT_BASE}/{statement_id}"
                stmt_raw = self._request("GET", stmt_path)
                enriched = self.parse_query_result_response(stmt_raw)
                # Merge: keep schema from step 1, override rows from step 2
                if enriched.columns:
                    result.columns = enriched.columns
                result.rows = enriched.rows[:row_limit]
                result.row_count = len(result.rows)
                result.total_row_count = enriched.total_row_count or result.row_count
                result.status = enriched.status or result.status
            except GenieClientError as e:
                logger.warning(
                    "Could not fetch rows from Statement Execution API (%s): %s",
                    statement_id,
                    str(e)[:150],
                )
                # Non-fatal — return schema-only result

        return result

    # -------------------------------------------------------------------------
    # PUBLIC API — Attachment extraction helpers
    # -------------------------------------------------------------------------

    def extract_text_attachments(
        self, message: GenieMessage
    ) -> List[GenieTextAttachment]:
        """Return all text (natural language) attachments from a message."""
        return list(message.text_attachments)

    def extract_query_attachments(
        self, message: GenieMessage
    ) -> List[GenieQueryAttachment]:
        """Return all SQL query attachments from a message."""
        return list(message.query_attachments)

    def extract_viz_attachments(
        self, message: GenieMessage
    ) -> List[GenieVizAttachment]:
        """Return all visualization attachments from a message.

        Note: Genie viz attachments contain only a ``query_attachment_id``
        reference. No chart spec is available via the API. See module docstring.
        """
        return list(message.viz_attachments)

    def extract_suggested_questions(
        self, message: GenieMessage
    ) -> List[str]:
        """Return follow-up question suggestions from a message."""
        return list(message.suggested_questions)

    def extract_generated_sql(self, message: GenieMessage) -> Optional[str]:
        """Return the first generated SQL string from a message, or None."""
        queries = self.extract_query_attachments(message)
        if queries:
            return queries[0].sql or None
        return None

    def extract_statement_ids(self, message: GenieMessage) -> List[str]:
        """Return all statement IDs from query attachments in a message."""
        return [
            q.statement_id
            for q in message.query_attachments
            if q.statement_id
        ]

    # -------------------------------------------------------------------------
    # PUBLIC API — Response parsing
    # -------------------------------------------------------------------------

    def parse_query_result_response(
        self, response: Dict[str, Any]
    ) -> GenieQueryResult:
        """Parse a raw query-result or statement-execution API response.

        Handles two formats:
        1. Genie ``/query-result`` endpoint: wraps result in
           ``statement_response``.
        2. Databricks Statement Execution API ``GET /sql/statements/{id}``:
           top-level keys ``statement_id``, ``status``, ``manifest``,
           ``result``.
        """
        # Unwrap Genie envelope if present
        body = response.get("statement_response", response)

        statement_id = body.get("statement_id", "")

        # Status
        status_obj = body.get("status", {})
        status = (
            status_obj.get("state", "")
            if isinstance(status_obj, dict)
            else str(status_obj)
        )

        # Error message (if any)
        error = None
        if isinstance(status_obj, dict) and status_obj.get("error"):
            error = status_obj["error"].get("message", "Unknown error")

        # Schema (columns)
        columns: List[Dict[str, Any]] = []
        manifest = body.get("manifest", {})
        schema = manifest.get("schema", {})
        fmt = manifest.get("format", "UNKNOWN")
        for col in schema.get("columns", []):
            columns.append(
                {
                    "name": col.get("name", ""),
                    "type_text": col.get("type_text", ""),
                    "type_name": col.get("type_name", ""),
                    "position": col.get("position", 0),
                }
            )

        # Row data (present in Statement Execution API responses)
        rows: List[List[Any]] = []
        result_obj = body.get("result", {})
        if isinstance(result_obj, dict):
            data_array = result_obj.get("data_array", [])
            if data_array:
                rows = list(data_array)

        # Total row count from manifest
        total_row_count = manifest.get("total_row_count") or len(rows) or None

        return GenieQueryResult(
            statement_id=statement_id,
            status=status,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            total_row_count=total_row_count,
            format=fmt,
            error=error,
        )

    # -------------------------------------------------------------------------
    # INTERNAL — Message parsing
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_message(raw: Dict[str, Any]) -> GenieMessage:
        """Convert a raw Genie message API response into a GenieMessage."""
        msg_id = raw.get("id") or raw.get("message_id", "")
        text_attachments: List[GenieTextAttachment] = []
        query_attachments: List[GenieQueryAttachment] = []
        viz_attachments: List[GenieVizAttachment] = []
        suggested_questions: List[str] = []

        for att in raw.get("attachments", []):
            att_id = att.get("attachment_id", "")

            if "text" in att:
                text_block = att["text"]
                content = (
                    text_block.get("content", "")
                    if isinstance(text_block, dict)
                    else str(text_block)
                )
                text_attachments.append(
                    GenieTextAttachment(content=content, attachment_id=att_id or None)
                )

            elif "query" in att:
                q = att["query"]
                qrm = q.get("query_result_metadata", {})
                query_attachments.append(
                    GenieQueryAttachment(
                        attachment_id=att_id,
                        sql=q.get("query", ""),
                        description=q.get("description", ""),
                        statement_id=q.get("statement_id", ""),
                        row_count=(
                            qrm.get("row_count")
                            if isinstance(qrm, dict)
                            else None
                        ),
                        thoughts=q.get("thoughts", []),
                    )
                )

            elif "viz" in att:
                v = att["viz"]
                # Genie viz has only a reference pointer — no chart spec
                viz_attachments.append(
                    GenieVizAttachment(
                        attachment_id=att_id,
                        query_attachment_id=(
                            v.get("query_attachment_id", "")
                            if isinstance(v, dict)
                            else ""
                        ),
                    )
                )

            elif "suggested_questions" in att:
                sq_block = att["suggested_questions"]
                if isinstance(sq_block, dict):
                    suggested_questions.extend(
                        sq_block.get("questions", [])
                    )

            else:
                # Unknown attachment type — log at debug, ignore
                unknown_keys = [k for k in att if k != "attachment_id"]
                logger.debug(
                    "Unknown Genie attachment type(s): %s", unknown_keys
                )

        return GenieMessage(
            id=msg_id,
            space_id=raw.get("space_id", ""),
            conversation_id=raw.get("conversation_id", ""),
            status=raw.get("status", ""),
            content=raw.get("content", ""),
            message_id=msg_id,
            user_id=raw.get("user_id"),
            created_timestamp=raw.get("created_timestamp"),
            last_updated_timestamp=raw.get("last_updated_timestamp"),
            raw_attachments=raw.get("attachments", []),
            raw_query_result=raw.get("query_result"),
            text_attachments=text_attachments,
            query_attachments=query_attachments,
            viz_attachments=viz_attachments,
            suggested_questions=suggested_questions,
        )
