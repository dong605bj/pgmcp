"""Unit tests for the metrics pipeline: business flows must record metrics."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import SecurityViolationError
from pg_mcp.models.query import QueryRequest, ResultValidationResult
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.observability.metrics import metrics
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_generator import GenerationResult


def _sample(name: str, **labels) -> float | None:
    """Read a Prometheus sample value from the default registry."""
    return REGISTRY.get_sample_value(name, labels)


def _make_orchestrator(
    generator: AsyncMock,
    validator: MagicMock,
    executor: AsyncMock | None = None,
    result_validator: AsyncMock | None = None,
) -> QueryOrchestrator:
    """Build an orchestrator wired with the given mocks."""
    if executor is None:
        executor = AsyncMock()
        executor.execute.return_value = ([{"id": 1}], 1)
    if result_validator is None:
        result_validator = AsyncMock()
        result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="ok",
            suggestion=None,
            is_acceptable=True,
            tokens_used=7,
        )
    cache = MagicMock()
    cache.get.return_value = DatabaseSchema(database_name="test_db", tables=[], version="15.0")
    return QueryOrchestrator(
        sql_generator=generator,
        sql_validator=validator,
        executors={"test_db": executor},
        result_validator=result_validator,
        schema_cache=cache,
        pools={"test_db": MagicMock()},
        default_database="test_db",
        resilience_config=ResilienceConfig(max_retries=2),
        validation_config=ValidationConfig(enabled=True),
    )


class TestQueryMetrics:
    """Successful/failed requests must land in query metrics."""

    @pytest.mark.asyncio
    async def test_success_request_counters(self) -> None:
        generator = AsyncMock()
        generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT 1;", tokens_used=123
        )
        validator = MagicMock()
        validator.validate_or_raise.return_value = None
        validator.blocked_tables = set()
        validator.extract_tables.return_value = []

        before_requests = _sample(
            "pg_mcp_query_requests_total", status="success", database="test_db"
        )
        before_duration = _sample("pg_mcp_query_duration_seconds_count")

        orchestrator = _make_orchestrator(generator, validator)
        response = await orchestrator.execute_query(
            QueryRequest(question="how many?", database="test_db")
        )

        assert response.success is True
        assert response.tokens_used == 130  # 123 generation + 7 validation

        after_requests = _sample(
            "pg_mcp_query_requests_total", status="success", database="test_db"
        )
        after_duration = _sample("pg_mcp_query_duration_seconds_count")

        assert (after_requests or 0) - (before_requests or 0) == 1
        assert (after_duration or 0) - (before_duration or 0) == 1

    @pytest.mark.asyncio
    async def test_security_rejection_counter(self) -> None:
        generator = AsyncMock()
        generator.generate_with_usage.return_value = GenerationResult(
            sql="DELETE FROM users;", tokens_used=10
        )
        validator = MagicMock()
        validator.validate_or_raise.side_effect = SecurityViolationError(
            "DELETE statements are not allowed"
        )
        validator.blocked_tables = set()

        before = _sample("pg_mcp_sql_rejected_total", reason="SecurityViolationError")

        orchestrator = _make_orchestrator(generator, validator)
        response = await orchestrator.execute_query(
            QueryRequest(question="delete everything", database="test_db")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "security_violation"

        after = _sample("pg_mcp_sql_rejected_total", reason="SecurityViolationError")
        # max_retries=2 → 3 attempts, each rejection recorded
        assert (after or 0) - (before or 0) == 3

    @pytest.mark.asyncio
    async def test_error_status_recorded(self) -> None:
        generator = AsyncMock()
        generator.generate_with_usage.return_value = GenerationResult(
            sql="DELETE FROM users;", tokens_used=0
        )
        validator = MagicMock()
        validator.validate_or_raise.side_effect = SecurityViolationError("nope")
        validator.blocked_tables = set()

        before = _sample(
            "pg_mcp_query_requests_total", status="security_violation", database="test_db"
        )

        orchestrator = _make_orchestrator(generator, validator)
        await orchestrator.execute_query(QueryRequest(question="delete", database="test_db"))

        after = _sample(
            "pg_mcp_query_requests_total", status="security_violation", database="test_db"
        )
        assert (after or 0) - (before or 0) == 1


class TestMetricsCollectorContract:
    """The collector helpers exist and record into the registry."""

    def test_increment_query_request(self) -> None:
        before = _sample("pg_mcp_query_requests_total", status="probe", database="probe_db")
        metrics.increment_query_request(status="probe", database="probe_db")
        after = _sample("pg_mcp_query_requests_total", status="probe", database="probe_db")
        assert (after or 0) - (before or 0) == 1

    def test_increment_llm_tokens(self) -> None:
        before = _sample("pg_mcp_llm_tokens_used_total", operation="generate_sql")
        metrics.increment_llm_tokens(operation="generate_sql", tokens=42)
        after = _sample("pg_mcp_llm_tokens_used_total", operation="generate_sql")
        assert (after or 0) - (before or 0) == 42

    def test_increment_sql_rejected(self) -> None:
        before = _sample("pg_mcp_sql_rejected_total", reason="probe_reason")
        metrics.increment_sql_rejected(reason="probe_reason")
        after = _sample("pg_mcp_sql_rejected_total", reason="probe_reason")
        assert (after or 0) - (before or 0) == 1
