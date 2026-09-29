"""SQL generation service using OpenAI for natural language to SQL conversion.

This module provides the SQLGenerator class that uses OpenAI's LLM to convert
natural language questions into valid PostgreSQL SQL queries.
"""

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from pg_mcp.config.settings import OpenAIConfig
from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.metrics import metrics as default_metrics
from pg_mcp.prompts.sql_generation import SQL_GENERATION_SYSTEM_PROMPT, build_user_prompt

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion

    from pg_mcp.models.schema import DatabaseSchema


@dataclass(frozen=True)
class GenerationResult:
    """SQL generation outcome including LLM usage.

    Attributes:
        sql: Generated SQL query (with trailing semicolon).
        tokens_used: Total tokens reported by the LLM (0 when unavailable).
    """

    sql: str
    tokens_used: int = 0


class SQLGenerator:
    """SQL generator using OpenAI for natural language to SQL conversion.

    This class handles the interaction with OpenAI's API to generate SQL queries
    from natural language questions. It includes robust error handling, SQL extraction
    from various response formats, and support for retry scenarios with error feedback.

    Example:
        >>> config = OpenAIConfig(api_key="sk-...", model="gpt-4")
        >>> generator = SQLGenerator(config)
        >>> result = await generator.generate_with_usage(
        ...     question="How many users registered today?",
        ...     schema=db_schema
        ... )
        >>> print(result.sql, result.tokens_used)
    """

    def __init__(
        self,
        config: OpenAIConfig,
        metrics: MetricsCollector | None = None,
    ) -> None:
        """Initialize SQL generator with OpenAI configuration.

        Args:
            config: OpenAI configuration including API key and model settings.
            metrics: Metrics collector (defaults to the shared singleton).
        """
        self.config = config
        self.metrics = metrics if metrics is not None else default_metrics
        self.client = AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            timeout=config.timeout,
        )

    async def generate(
        self,
        question: str,
        schema: "DatabaseSchema",
        context: str | None = None,
        previous_attempt: str | None = None,
        error_feedback: str | None = None,
    ) -> str:
        """Generate SQL statement from natural language question.

        Convenience wrapper around :meth:`generate_with_usage` that returns
        only the SQL string.

        Args:
            question: User's natural language question.
            schema: Database schema information for context.
            context: Optional additional context to guide generation.
            previous_attempt: Previously generated SQL that failed (for retry).
            error_feedback: Error message from previous attempt (for retry).

        Returns:
            str: Generated SQL query (without trailing semicolon).

        Raises:
            LLMError: If generation fails or response is invalid.
            LLMTimeoutError: If the API request times out.
            LLMUnavailableError: If the API is unavailable or authentication fails.
        """
        return (
            await self.generate_with_usage(
                question=question,
                schema=schema,
                context=context,
                previous_attempt=previous_attempt,
                error_feedback=error_feedback,
            )
        ).sql

    async def generate_with_usage(
        self,
        question: str,
        schema: "DatabaseSchema",
        context: str | None = None,
        previous_attempt: str | None = None,
        error_feedback: str | None = None,
    ) -> GenerationResult:
        """Generate SQL from natural language and report LLM token usage.

        This method sends the question and database schema to OpenAI's API
        and extracts the generated SQL query from the response. It supports
        retry scenarios by accepting previous failed attempts and error feedback.

        Args:
            question: User's natural language question.
            schema: Database schema information for context.
            context: Optional additional context to guide generation.
            previous_attempt: Previously generated SQL that failed (for retry).
            error_feedback: Error message from previous attempt (for retry).

        Returns:
            GenerationResult: Generated SQL and token usage.

        Raises:
            LLMError: If generation fails or response is invalid.
            LLMTimeoutError: If the API request times out.
            LLMUnavailableError: If the API is unavailable or authentication fails.

        Example:
            >>> # Retry with error feedback
            >>> result = await generator.generate_with_usage(
            ...     question="Count all active users",
            ...     schema=db_schema,
            ...     previous_attempt="SELECT COUNT(*) FROM user",
            ...     error_feedback='relation "user" does not exist'
            ... )
        """
        user_prompt = build_user_prompt(
            question=question,
            schema=schema,
            context=context,
            previous_attempt=previous_attempt,
            error_feedback=error_feedback,
        )

        self.metrics.increment_llm_call("generate_sql")
        start_time = time.monotonic()
        try:
            response: ChatCompletion = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": SQL_GENERATION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                # Disable Qwen3 thinking mode so the SQL is returned in `content`
                # instead of being consumed by the reasoning field.
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        except TimeoutError as e:
            self.metrics.observe_llm_latency("generate_sql", time.monotonic() - start_time)
            raise LLMTimeoutError(
                message=f"OpenAI API request timed out after {self.config.timeout}s",
                details={"timeout": self.config.timeout},
            ) from e
        except Exception as e:
            self.metrics.observe_llm_latency("generate_sql", time.monotonic() - start_time)
            # Handle various OpenAI errors
            error_msg = str(e)
            if "authentication" in error_msg.lower() or "api_key" in error_msg.lower():
                raise LLMUnavailableError(
                    message="OpenAI API authentication failed - check API key",
                    details={"error": error_msg},
                ) from e
            if "rate_limit" in error_msg.lower():
                raise LLMUnavailableError(
                    message="OpenAI API rate limit exceeded",
                    details={"error": error_msg},
                ) from e
            raise LLMError(
                message=f"OpenAI API request failed: {error_msg}",
                details={"error": error_msg},
            ) from e
        self.metrics.observe_llm_latency("generate_sql", time.monotonic() - start_time)

        tokens_used = 0
        usage = getattr(response, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        if isinstance(total_tokens, int) and total_tokens >= 0:
            tokens_used = total_tokens
            self.metrics.increment_llm_tokens("generate_sql", tokens_used)

        # Extract SQL from response
        if not response.choices:
            raise LLMError(
                message="OpenAI returned empty response",
                details={"response": response.model_dump()},
            )

        content = response.choices[0].message.content
        if not content:
            raise LLMError(
                message="OpenAI returned empty message content",
                details={"response": response.model_dump()},
            )

        sql = self._extract_sql(content)
        if not sql:
            raise LLMError(
                message="Failed to extract SQL from OpenAI response",
                details={"content": content},
            )

        return GenerationResult(sql=sql, tokens_used=tokens_used)

    def _extract_sql(self, content: str) -> str | None:
        """Extract SQL query from LLM response content.

        This method implements a multi-strategy approach to extract SQL from
        various response formats that LLMs might generate:

        1. Try to match ```sql ... ``` code blocks (preferred format)
        2. Try to match generic ``` ... ``` code blocks
        3. Try to find SELECT/WITH statements in plain text
        4. Check if entire content looks like SQL

        Args:
            content: Raw content from LLM response.

        Returns:
            str | None: Extracted SQL query, or None if extraction fails.

        Example:
            >>> generator._extract_sql("```sql\\nSELECT 1;\\n```")
            'SELECT 1;'
            >>> generator._extract_sql("SELECT * FROM users;")
            'SELECT * FROM users;'
        """
        if not content:
            return None

        content = content.strip()

        # Strategy 1: Match ```sql ... ``` or ``` ... ``` code blocks
        code_block_pattern = r"```(?:sql)?\s*\n?(.*?)\n?```"
        matches = re.findall(code_block_pattern, content, re.DOTALL | re.IGNORECASE)

        if matches:
            sql = matches[0].strip()
            # Remove trailing semicolon for consistency
            return sql.rstrip(";") + ";"

        # Strategy 2: Find SELECT/WITH statements in plain text
        sql_pattern = r"((?:WITH|SELECT)\s+.*?)(?:;|$)"
        matches = re.findall(sql_pattern, content, re.DOTALL | re.IGNORECASE)

        if matches:
            sql = matches[0].strip()
            return sql.rstrip(";") + ";"

        # Strategy 3: Check if entire content looks like SQL
        if content.upper().startswith(("SELECT", "WITH")):
            return content.rstrip(";") + ";"

        return None
