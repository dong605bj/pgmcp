"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It integrates the resilience layer (rate limiting, retry with
exponential backoff, circuit breaker) and the observability layer (request
tracing via contextvars, Prometheus metrics) into the request path.
"""

import asyncio
import time
from typing import Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMTimeoutError,
    LLMUnavailableError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorInfo,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.logging import get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.metrics import metrics as default_metrics
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_logger(__name__)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation. It implements retry logic with exponential backoff and error
    feedback, a circuit breaker for fault tolerance, per-request rate limiting,
    request tracing via contextvars, and Prometheus metrics.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validator=validator,
        ...     executors={"mydb": executor},
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ...     default_database="mydb",
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        executors: dict[str, SQLExecutor],
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        default_database: str | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validator: SQL validation service.
            executors: Executors keyed by database name; the executor matching
                the resolved database is always used (multi-database routing).
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries, backoff,
                concurrency limits and circuit breaker.
            validation_config: Validation configuration including thresholds.
            default_database: Database used when a request specifies none
                (defaults to the first executor key).
            rate_limiter: Rate limiter for queries and LLM calls; created from
                ``resilience_config`` when omitted.
            circuit_breaker: Shared circuit breaker; created from
                ``resilience_config`` when omitted.
            metrics: Metrics collector (defaults to the shared singleton).
        """
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.executors = executors
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.default_database = default_database or next(iter(executors), None)
        self.metrics = metrics if metrics is not None else default_metrics

        if rate_limiter is not None:
            self.rate_limiter = rate_limiter
        else:
            self.rate_limiter = MultiRateLimiter(
                query_limit=resilience_config.max_concurrent_queries,
                llm_limit=resilience_config.max_concurrent_llm,
            )

        if circuit_breaker is not None:
            self.circuit_breaker = circuit_breaker
        else:
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=resilience_config.circuit_breaker_threshold,
                recovery_timeout=resilience_config.circuit_breaker_timeout,
            )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        The whole flow is wrapped in a query rate-limit slot and a tracing
        request context. The pipeline:

        1. Acquire rate limit slot (reject with RATE_LIMITED when saturated)
        2. Generate request_id for full-chain tracing
        3. Resolve and validate database name
        4. Load schema from cache
        5. Generate and validate SQL with retry + exponential backoff
        6. Execute SQL on the executor bound to the resolved database
        7. Validate results (optional, non-blocking)
        8. Return structured response carrying the request_id

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.
        """
        started_at = time.monotonic()
        # Bound here so it survives the request_context() reset on exit
        request_id: str | None = None
        try:
            async with (
                self.rate_limiter.for_queries(timeout=self.resilience_config.rate_limit_timeout),
                request_context() as ctx_request_id,
            ):
                request_id = ctx_request_id
                response = await self._process_query(request, request_id)
        except TimeoutError:
            # The query rate limiter slot timed out — reject with RATE_LIMITED
            logger.warning(
                "Query rejected: rate limiter timeout",
                extra={
                    "request_id": request_id,
                    "timeout_seconds": self.resilience_config.rate_limit_timeout,
                },
            )
            response = QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorInfo(
                    code=ErrorCode.RATE_LIMIT_EXCEEDED.value,
                    message=(
                        "Too many concurrent queries; no slot became available "
                        f"within {self.resilience_config.rate_limit_timeout}s"
                    ),
                    details={"rate_limit_timeout": self.resilience_config.rate_limit_timeout},
                ),
                confidence=0,
                tokens_used=None,
            )
        except PgMcpError as e:
            logger.warning(
                "Query rejected before processing",
                extra={"request_id": request_id, "error_code": e.code, "error_message": e.message},
            )
            response = QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorInfo(code=e.code.value, message=e.message, details=e.details),
                confidence=0,
                tokens_used=None,
            )
        except Exception as e:
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            response = QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=None,
            )

        # Full-chain metrics: one sample per request
        duration = time.monotonic() - started_at
        self.metrics.query_duration.observe(duration)
        status = (
            "success" if response.success else (response.error.code if response.error else "error")
        )
        database = request.database or self.default_database or "unknown"
        self.metrics.increment_query_request(status=status, database=database)

        response.request_id = request_id
        return response

    async def _process_query(self, request: QueryRequest, request_id: str) -> QueryResponse:
        """Run generation/validation/execution and build the response.

        Args:
            request: Query request containing question and parameters.
            request_id: Correlation ID for logging and tracing.

        Returns:
            QueryResponse: Response with results or structured error details.
        """
        logger.info(
            "Starting query execution",
            extra={"request_id": request_id, "question": request.question[:100]},
        )

        try:
            # Step 1: Resolve database name
            database_name = self._resolve_database(request.database)
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 2: Get schema from cache
            schema = await self._load_schema(database_name, request_id)
            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 3: Generate and validate SQL with retry + backoff
            generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
                question=request.question,
                schema=schema,
                request_id=request_id,
            )

            # Step 4: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                )

            # Step 5: Execute SQL on the executor bound to the resolved database.
            # Double-check blocked tables before execution to guard against
            # parser drift between validation and execution time.
            self._recheck_blocked_tables(generated_sql)
            executor = self.executors.get(database_name)
            if executor is None:
                raise DatabaseError(
                    message=f"No SQL executor available for database '{database_name}'",
                    details={"database": database_name},
                )

            logger.debug("Executing SQL", extra={"request_id": request_id})
            start_time = time.monotonic()
            results, total_count = await executor.execute(generated_sql)
            execution_time_ms = (time.monotonic() - start_time) * 1000
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )

            # Step 6: Validate results (non-blocking, failures don't fail the request)
            result_confidence, validation_tokens = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 7: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=(tokens_used or 0) + (validation_tokens or 0),
            )

        except PgMcpError as e:
            # Handle known application errors
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": e.message,
                },
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorInfo(code=e.code.value, message=e.message, details=e.details),
                confidence=0,
                tokens_used=None,
            )
        except Exception as e:
            # Handle unexpected errors
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=None,
            )

    async def _load_schema(self, database_name: str, request_id: str) -> Any:
        """Load the schema for a database from cache (introspecting on miss).

        Args:
            database_name: Resolved database name.
            request_id: Correlation ID for logging.

        Returns:
            DatabaseSchema for the database.

        Raises:
            DatabaseError: If no pool exists for the database.
            SchemaLoadError: If introspection fails.
        """
        schema = self.schema_cache.get(database_name)
        if schema is not None:
            return schema

        pool = self.pools.get(database_name)
        if pool is None:
            raise DatabaseError(
                message=f"No connection pool available for database '{database_name}'",
                details={"database": database_name},
            )
        try:
            return await self.schema_cache.load(database_name, pool)
        except Exception as e:
            raise SchemaLoadError(
                message=f"Failed to load schema for database '{database_name}': {e!s}",
                details={"database": database_name, "error": str(e)},
            ) from e

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or fall back to the default.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or no default exists.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Returns default database
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.executors:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.executors.keys()),
                    },
                )
            return database

        if not self.default_database or self.default_database not in self.executors:
            raise DatabaseError(
                message="No databases configured",
                details={"available_databases": list(self.executors.keys())},
            )
        return self.default_database

    def _recheck_blocked_tables(self, sql: str) -> None:
        """Defensive re-check of blocked tables just before execution.

        Validation already rejects blocked tables at parse time; this second
        pass protects against parser drift between the validator's sqlglot
        version and runtime behavior.

        Args:
            sql: Generated SQL about to be executed.

        Raises:
            SecurityViolationError: If a blocked table name is referenced.
            SQLParseError: If the SQL cannot be re-parsed (fail closed).
        """
        blocked_bare = {entry.split(".")[-1].lower() for entry in self.sql_validator.blocked_tables}
        if not blocked_bare:
            return
        referenced = set(self.sql_validator.extract_tables(sql))
        violated = referenced & blocked_bare
        if violated:
            raise SecurityViolationError(
                f"Access to blocked table(s) {sorted(violated)} detected before execution"
            )

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[str, ValidationResult, int]:
        """Generate and validate SQL with backoff retry on failures.

        Each attempt acquires an LLM rate-limit slot. Validation failures retry
        with error feedback; transient LLM errors (timeout/unavailability)
        retry after an exponential backoff delay
        (``retry_delay * backoff_factor ** attempt``). All outcomes feed the
        circuit breaker.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            RateLimitExceededError: If no LLM slot frees up in time.
            LLMError: If circuit breaker is open or generation keeps failing.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used = 0

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                # Generate SQL (LLM call bounded by the LLM rate limiter)
                async with self.rate_limiter.for_llm(
                    timeout=self.resilience_config.rate_limit_timeout
                ):
                    generation = await self.sql_generator.generate_with_usage(
                        question=question,
                        schema=schema,
                        previous_attempt=previous_sql,
                        error_feedback=error_feedback,
                    )
                generated_sql = generation.sql
                tokens_used = generation.tokens_used

                logger.debug(
                    "SQL generated",
                    extra={
                        "request_id": request_id,
                        "sql_length": len(generated_sql),
                        "tokens_used": tokens_used,
                    },
                )

                # Validate SQL
                try:
                    self.sql_validator.validate_or_raise(generated_sql)
                except (SecurityViolationError, SQLParseError) as validation_error:
                    if attempt < max_retries:
                        # Record as failure and retry with feedback
                        self.metrics.increment_sql_rejected(reason=type(validation_error).__name__)
                        logger.warning(
                            "SQL validation failed, retrying with feedback",
                            extra={
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error": str(validation_error),
                            },
                        )
                        previous_sql = generated_sql
                        error_feedback = str(validation_error)
                        await self._sleep_backoff(attempt)
                        continue

                    # Out of retries, record failure and raise
                    self.metrics.increment_sql_rejected(reason=type(validation_error).__name__)
                    self.circuit_breaker.record_failure()
                    logger.error(
                        "SQL validation failed after all retries",
                        extra={
                            "request_id": request_id,
                            "attempts": attempt + 1,
                            "error": str(validation_error),
                        },
                    )
                    raise

                # Validation successful
                self.circuit_breaker.record_success()
                logger.info(
                    "SQL generated and validated successfully",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                    },
                )

                # Build validation result
                validation_result = ValidationResult(
                    is_valid=True,
                    is_select=True,
                    allows_data_modification=False,
                    uses_blocked_functions=[],
                    error_message=None,
                )

                return generated_sql, validation_result, tokens_used

            except RateLimitExceededError:
                # No LLM slot became available in time — bubble up as-is
                raise
            except TimeoutError as e:
                # The LLM rate limiter slot timed out — reject as RATE_LIMITED
                self.circuit_breaker.record_failure()
                raise RateLimitExceededError(
                    message="No LLM slot became available within the rate limit timeout",
                    details={"rate_limit_timeout": self.resilience_config.rate_limit_timeout},
                ) from e
            except (LLMTimeoutError, LLMUnavailableError) as transient_error:
                # Transient LLM failures are retriable after backoff
                self.circuit_breaker.record_failure()
                if attempt < max_retries:
                    logger.warning(
                        "Transient LLM failure, retrying after backoff",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "error": str(transient_error),
                        },
                    )
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(
                    "Transient LLM failure after all retries",
                    extra={"request_id": request_id, "attempts": attempt + 1},
                )
                raise
            except (LLMError, SecurityViolationError, SQLParseError):
                # Re-raise known non-retriable errors
                raise
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        """Sleep for the exponential backoff delay of this attempt.

        Args:
            attempt: Zero-based attempt index that just failed.
        """
        delay = self.resilience_config.retry_delay * (
            self.resilience_config.backoff_factor**attempt
        )
        logger.debug("Backing off before next retry", extra={"delay_seconds": delay})
        await asyncio.sleep(delay)

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> tuple[int, int]:
        """Validate query results with error handling (non-blocking).

        The LLM call is bounded by the LLM rate limiter. Failures don't cause
        the overall query to fail.

        Args:
            question: User's original natural language question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            tuple[int, int]: (confidence score 0-100, tokens used; confidence
            defaults to 100 and tokens to 0 when validation is disabled/fails).
        """
        if not self.validation_config.enabled:
            return 100, 0

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            async with self.rate_limiter.for_llm(timeout=self.resilience_config.rate_limit_timeout):
                validation_result = await self.result_validator.validate(
                    question=question,
                    sql=sql,
                    results=results,
                    row_count=row_count,
                )

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return validation_result.confidence, validation_result.tokens_used or 0

        except TimeoutError:
            # Rate limiter slot timeout — validation is best-effort, skip it
            logger.warning(
                "Result validation skipped: LLM rate limiter timeout",
                extra={"request_id": request_id},
            )
            return 100, 0
        except Exception as e:
            # Log but don't fail the query
            logger.warning(
                "Result validation failed, continuing with default confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return 100, 0  # Default to high confidence if validation fails

    @staticmethod
    def _get_current_time_ms() -> float:
        """Get current time in milliseconds (monotonic, for durations only).

        Returns:
            float: Monotonic clock reading in milliseconds.
        """
        return time.monotonic() * 1000
