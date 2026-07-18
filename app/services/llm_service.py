"""LLM service abstraction layer.

Provides a unified interface for all LLM interactions via Databricks Model Serving.
Endpoints are configured through environment variables - never hardcoded.

Capabilities:
- Intent classification (fast endpoint)
- SQL generation (primary endpoint)
- SQL repair on failure (primary endpoint)
- Grounded result summarization (primary endpoint)

Uses OpenAI-compatible client since Databricks Model Serving exposes
the /v1/chat/completions API.
"""

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError

from app.config import settings

logger = logging.getLogger(__name__)


# =============================================================================
# DATA MODELS
# =============================================================================


class Intent(Enum):
    """Classified intent of a user message."""
    SHIPMENT_QUERY = "shipment_query"
    GREETING = "greeting"
    CLARIFICATION_NEEDED = "clarification_needed"
    OFF_TOPIC = "off_topic"
    FOLLOW_UP = "follow_up"


@dataclass
class LLMResponse:
    """Structured response from an LLM call."""
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass
class SQLGenerationResult:
    """Result of SQL generation from natural language."""
    sql: str
    explanation: Optional[str] = None
    confidence: str = "high"  # high, medium, low


@dataclass
class IntentClassification:
    """Result of intent classification."""
    intent: Intent
    confidence: float
    reasoning: Optional[str] = None


# =============================================================================
# LLM SERVICE
# =============================================================================


class LLMService:
    """Manages all LLM interactions through Databricks Model Serving.
    
    Endpoint names come exclusively from environment variables.
    No endpoint names are ever hardcoded in this class.
    """

    def __init__(self):
        """Initialize OpenAI-compatible client for Databricks Model Serving."""
        if not settings.DATABRICKS_HOST:
            raise ValueError("DATABRICKS_HOST not configured")

        host = settings.DATABRICKS_HOST.rstrip("/")
        if not host.startswith("http"):
            host = f"https://{host}"
        self._base_url = f"{host}/serving-endpoints"
        self._timeout = settings.LLM_TIMEOUT_SECONDS

        # SDK client for token refresh (Databricks Apps OAuth tokens expire)
        from databricks.sdk import WorkspaceClient
        self._ws = WorkspaceClient()

        self._primary_endpoint = settings.LLM_ENDPOINT_PRIMARY
        self._fast_endpoint = settings.LLM_ENDPOINT_FAST

        if not self._primary_endpoint:
            logger.warning(
                "LLM_ENDPOINT_PRIMARY not set. SQL generation will fail until configured."
            )
        if not self._fast_endpoint:
            logger.info(
                "LLM_ENDPOINT_FAST not set. Intent classification will use primary endpoint."
            )

    @property
    def _client(self) -> OpenAI:
        """Get OpenAI client with a fresh token (refreshed on each call)."""
        token = settings.DATABRICKS_TOKEN
        if not token:
            # Retry auth up to 2 times (handles transient token refresh failures)
            for attempt in range(2):
                try:
                    headers = self._ws.config.authenticate()
                    for k, v in headers.items():
                        if k.lower() == "authorization":
                            token = v.replace("Bearer ", "")
                            break
                    if token:
                        break
                except Exception as e:
                    logger.warning(f"Auth attempt {attempt + 1} failed: {e}")
                    if attempt == 0:
                        import time as _time
                        _time.sleep(1)
                        # Reinitialize WorkspaceClient in case credentials refreshed
                        try:
                            from databricks.sdk import WorkspaceClient
                            self._ws = WorkspaceClient()
                        except Exception:
                            pass
                    else:
                        logger.error(f"All auth attempts failed: {e}")
                        raise RuntimeError(f"Failed to authenticate with Databricks: {e}") from e
        if not token:
            raise RuntimeError("No authentication token available. Check app service principal configuration.")
        return OpenAI(
            api_key=token,
            base_url=self._base_url,
            timeout=self._timeout,
        )

    # -------------------------------------------------------------------------
    # CORE CALL (with retry)
    # -------------------------------------------------------------------------

    def _call(
        self,
        endpoint: str,
        messages: List[dict],
        max_tokens: int,
        temperature: float = 0.0,
        retries: int = 2,
    ) -> LLMResponse:
        """Make an LLM call with retry logic.

        Args:
            endpoint: Model serving endpoint name.
            messages: Chat messages (system + user + optional assistant).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            retries: Number of retry attempts on transient failures.

        Returns:
            LLMResponse with content and metadata.

        Raises:
            ValueError: If endpoint is not configured.
            RuntimeError: If all retries exhausted.
        """
        if not endpoint:
            raise ValueError(
                "LLM endpoint not configured. Set LLM_ENDPOINT_PRIMARY and/or "
                "LLM_ENDPOINT_FAST environment variables."
            )

        last_error = None
        for attempt in range(retries + 1):
            try:
                start_ms = int(time.time() * 1000)
                # Build kwargs - some models (Claude) don't support temperature
                call_kwargs = {
                    "model": endpoint,
                    "messages": messages,
                    "max_tokens": max_tokens,
                }
                if temperature is not None:
                    call_kwargs["temperature"] = temperature

                try:
                    response = self._client.chat.completions.create(**call_kwargs)
                except Exception as temp_err:
                    # Retry without temperature if model doesn't support it
                    if "temperature" in str(temp_err).lower():
                        call_kwargs.pop("temperature", None)
                        response = self._client.chat.completions.create(**call_kwargs)
                    else:
                        raise
                elapsed_ms = int(time.time() * 1000) - start_ms

                choice = response.choices[0]
                usage = response.usage

                # Handle content as string or list of content blocks
                raw_content = choice.message.content
                if isinstance(raw_content, list):
                    # Content blocks format: [{"type": "text", "text": "..."}, ...]
                    # Filter to only text blocks - skip reasoning/thinking blocks
                    text_parts = []
                    for block in raw_content:
                        if isinstance(block, dict):
                            block_type = block.get("type", "")
                            if block_type == "text":
                                text_parts.append(block.get("text", ""))
                            elif block_type in ("reasoning", "thinking", "redacted"):
                                continue  # Skip reasoning blocks
                            else:
                                # Unknown block type - try to get text
                                if "text" in block:
                                    text_parts.append(block["text"])
                        elif hasattr(block, "text"):
                            # Object with .text attribute
                            if getattr(block, "type", "") not in ("reasoning", "thinking", "redacted"):
                                text_parts.append(block.text)
                        else:
                            text_parts.append(str(block))
                    final_content = "\n".join(text_parts)
                else:
                    final_content = raw_content or ""

                return LLMResponse(
                    content=final_content,
                    model=response.model or endpoint,
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    latency_ms=elapsed_ms,
                )

            except (APITimeoutError, APIConnectionError, RateLimitError) as e:
                last_error = e
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "LLM call to %s failed (attempt %d/%d): %s. Retrying in %ds...",
                        endpoint, attempt + 1, retries + 1, str(e)[:100], wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "LLM call to %s exhausted retries: %s", endpoint, str(e)[:200]
                    )

            except Exception as e:
                logger.error("LLM call to %s failed with unexpected error: %s", endpoint, e)
                raise RuntimeError(f"LLM call failed: {e}") from e

        raise RuntimeError(
            f"LLM call to {endpoint} failed after {retries + 1} attempts: {last_error}"
        )

    # -------------------------------------------------------------------------
    # INTENT CLASSIFICATION
    # -------------------------------------------------------------------------

    def classify_intent(self, user_message: str) -> IntentClassification:
        """Classify the intent of a user message.

        Uses the fast endpoint for low-latency classification.
        Falls back to primary endpoint if fast is not configured.

        Args:
            user_message: Raw user input.

        Returns:
            IntentClassification with intent enum and confidence.
        """
        endpoint = self._fast_endpoint or self._primary_endpoint

        system_prompt = (
            "You are an intent classifier for a shipment tracking chatbot. "
            "Classify the user message into exactly one category:\n"
            "- shipment_query: Questions about shipments, deliveries, tracking, delays, revenue, weights, status\n"
            "- greeting: Hello, hi, good morning, thanks, bye\n"
            "- follow_up: References to previous context like 'and for Germany?', 'what about last month?'\n"
            "- clarification_needed: Ambiguous query that needs more info before generating SQL\n"
            "- off_topic: Not related to shipments at all\n\n"
            "Respond with ONLY a JSON object: {\"intent\": \"<category>\", \"confidence\": <0.0-1.0>}"
        )

        response = self._call(
            endpoint=endpoint,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_tokens=100,
            temperature=0.0,
        )

        # Parse response
        try:
            import json
            # Extract JSON from response (handle markdown code blocks)
            content = response.content.strip()
            if content.startswith("```"):
                content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`")
            parsed = json.loads(content)
            intent = Intent(parsed.get("intent", "shipment_query"))
            confidence = float(parsed.get("confidence", 0.8))
        except (json.JSONDecodeError, ValueError):
            # Default to shipment_query if parsing fails
            logger.warning("Failed to parse intent response: %s", response.content[:100])
            intent = Intent.SHIPMENT_QUERY
            confidence = 0.5

        return IntentClassification(
            intent=intent,
            confidence=confidence,
            reasoning=None,
        )

    # -------------------------------------------------------------------------
    # SQL GENERATION
    # -------------------------------------------------------------------------

    def generate_sql(
        self,
        user_message: str,
        system_prompt: str,
        conversation_history: Optional[List[dict]] = None,
    ) -> SQLGenerationResult:
        """Generate SQL from a natural language query.

        Args:
            user_message: The user's natural language question.
            system_prompt: Full system prompt (from prompt_builder).
            conversation_history: Optional prior messages for context.

        Returns:
            SQLGenerationResult with extracted SQL.
        """
        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history for context (last 4 exchanges max)
        if conversation_history:
            messages.extend(conversation_history[-8:])

        messages.append({"role": "user", "content": user_message})

        response = self._call(
            endpoint=self._primary_endpoint,
            messages=messages,
            max_tokens=settings.LLM_MAX_TOKENS_SQL,
            temperature=settings.LLM_TEMPERATURE,
        )

        # Extract SQL from response (may be in code block or plain)
        sql = self._extract_sql(response.content)

        logger.info(
            "SQL generated in %dms (%d prompt tokens, %d completion tokens)",
            response.latency_ms, response.prompt_tokens, response.completion_tokens,
        )

        return SQLGenerationResult(sql=sql)

    # -------------------------------------------------------------------------
    # SQL REPAIR
    # -------------------------------------------------------------------------

    def repair_sql(
        self,
        original_sql: str,
        error_message: str,
        system_prompt: str,
        user_message: str,
    ) -> SQLGenerationResult:
        """Attempt to fix SQL that failed validation or execution.

        Args:
            original_sql: The SQL that failed.
            error_message: The error from validation or execution.
            system_prompt: Full system prompt for context.
            user_message: Original user question.

        Returns:
            SQLGenerationResult with repaired SQL.
        """
        repair_prompt = (
            f"The following SQL was generated for the user question: '{user_message}'\n\n"
            f"```sql\n{original_sql}\n```\n\n"
            f"But it produced this error:\n{error_message}\n\n"
            "Please fix the SQL to resolve the error. "
            "Return ONLY the corrected SQL in a code block. "
            "Remember to use Databricks SQL syntax: "
            "DATEDIFF(endDate, startDate) NOT DATEDIFF('day', ...)."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": repair_prompt},
        ]

        response = self._call(
            endpoint=self._primary_endpoint,
            messages=messages,
            max_tokens=settings.LLM_MAX_TOKENS_SQL,
            temperature=0.0,
        )

        sql = self._extract_sql(response.content)

        logger.info("SQL repaired in %dms", response.latency_ms)

        return SQLGenerationResult(sql=sql, explanation="Repaired after error")

    # -------------------------------------------------------------------------
    # RESULT SUMMARIZATION
    # -------------------------------------------------------------------------

    def summarize_results(
        self,
        user_message: str,
        sql: str,
        row_count: int,
        sample_rows: List[dict],
        headers: List[str],
    ) -> str:
        """Generate a natural language summary of query results.

        Args:
            user_message: Original user question.
            sql: The SQL that was executed.
            row_count: Total number of rows returned.
            sample_rows: First few rows as dicts for context.
            headers: Column names.

        Returns:
            Human-readable summary string.
        """
        # Build a compact representation of results
        if row_count == 0:
            result_context = "The query returned 0 rows (no matching data found)."
        else:
            sample_text = "\n".join(
                str(row) for row in sample_rows[:5]
            )
            result_context = (
                f"The query returned {row_count} row(s).\n"
                f"Columns: {', '.join(headers)}\n"
                f"Sample data (first {min(5, len(sample_rows))} rows):\n{sample_text}"
            )

        prompt = (
            f"The user asked: '{user_message}'\n\n"
            f"The SQL query executed was:\n```sql\n{sql}\n```\n\n"
            f"Results:\n{result_context}\n\n"
            "Provide a brief, helpful natural language summary of the results. "
            "Be concise (2-3 sentences max). "
            "Include key numbers or insights. "
            "Do not mention SQL or internal processing details."
        )

        response = self._call(
            endpoint=self._primary_endpoint,
            messages=[
                {"role": "system", "content": "You are a helpful shipment data analyst. Summarize query results clearly and concisely."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=settings.LLM_MAX_TOKENS_SUMMARY,
            temperature=0.1,
        )

        return response.content.strip()

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_sql(text: str) -> str:
        """Extract SQL from LLM response (handles code blocks and plain text).

        Args:
            text: Raw LLM response content.

        Returns:
            Clean SQL string.
        """
        # Try to extract from ```sql ... ``` block
        sql_block_match = re.search(
            r"```(?:sql)?\s*\n?(.*?)\n?```", text, re.DOTALL | re.IGNORECASE
        )
        if sql_block_match:
            return sql_block_match.group(1).strip()

        # Try to find a SELECT statement
        select_match = re.search(
            r"(SELECT\s+.+?)(?:;\s*$|$)", text, re.DOTALL | re.IGNORECASE
        )
        if select_match:
            return select_match.group(1).strip().rstrip(";")

        # Return as-is (likely just SQL without formatting)
        return text.strip().rstrip(";")

    def health_check(self) -> dict:
        """Check if the LLM service is properly configured."""
        return {
            "primary_endpoint": self._primary_endpoint or "(not configured)",
            "fast_endpoint": self._fast_endpoint or "(not configured)",
            "host": settings.DATABRICKS_HOST,
            "status": "ready" if self._primary_endpoint else "unconfigured",
        }
