"""Unit tests for QueryOrchestrator.

This module tests the orchestrator's coordination of the query pipeline,
including retry logic, error handling, and integration with all components.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    LLMError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    QueryRequest,
    ResultValidationResult,
    ReturnType,
)
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.resilience.circuit_breaker import CircuitState
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_generator import GenerationResult


class TestDatabaseResolution:
    """Test database name resolution logic."""

    @pytest.fixture
    def mock_pools(self) -> dict[str, MagicMock]:
        """Create mock connection pools."""
        return {
            "db1": MagicMock(),
            "db2": MagicMock(),
        }

    @pytest.fixture
    def orchestrator(self, mock_pools: dict[str, MagicMock]) -> QueryOrchestrator:
        """Create orchestrator with mocked components."""
        return QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"db1": MagicMock(), "db2": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools=mock_pools,
            default_database="db1",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

    def test_resolve_database_specified_valid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified valid database."""
        result = orchestrator._resolve_database("db1")
        assert result == "db1"

    def test_resolve_database_specified_invalid(self, orchestrator: QueryOrchestrator) -> None:
        """Test resolving a specified but invalid database."""
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database("nonexistent")

        assert "not found" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]
        assert "db2" in exc_info.value.details["available_databases"]

    def test_resolve_database_auto_select_single(self) -> None:
        """Test auto-selecting when only one database available."""
        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"only_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"only_db": MagicMock()},
            default_database="only_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        result = orchestrator._resolve_database(None)
        assert result == "only_db"

    def test_resolve_database_multiple_uses_default(self, orchestrator: QueryOrchestrator) -> None:
        """Test that with multiple databases, None resolves to the default database."""
        result = orchestrator._resolve_database(None)
        assert result == "db1"

    def test_resolve_database_no_databases(self) -> None:
        """Test error when no databases configured."""
        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={},
            default_database=None,
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "no databases configured" in str(exc_info.value).lower()


class TestSQLGenerationWithRetry:
    """Test SQL generation with retry logic."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(
            database_name="test_db",
            tables=[
                TableInfo(
                    schema_name="public",
                    table_name="users",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="integer",
                            is_nullable=False,
                            is_primary_key=True,
                        ),
                        ColumnInfo(
                            name="name",
                            data_type="varchar(255)",
                            is_nullable=False,
                        ),
                    ],
                )
            ],
            version="15.0",
        )

    @pytest.mark.asyncio
    async def test_generate_sql_success_first_attempt(self, mock_schema: DatabaseSchema) -> None:
        """Test successful SQL generation on first attempt."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT * FROM users;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None  # No exception = valid

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(max_retries=3),
            validation_config=ValidationConfig(),
        )

        # Execute
        sql, validation_result, _tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            request_id="test-123",
        )

        # Verify
        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        assert validation_result.is_select is True
        mock_generator.generate_with_usage.assert_called_once()
        mock_validator.validate_or_raise.assert_called_once_with("SELECT * FROM users;")

    @pytest.mark.asyncio
    async def test_generate_sql_retry_on_validation_failure(
        self, mock_schema: DatabaseSchema
    ) -> None:
        """Test retry logic when validation fails."""
        # Setup mocks - first attempt fails validation, second succeeds
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.side_effect = [
            GenerationResult(
                sql="SELECT * FROM user;", tokens_used=0
            ),  # First attempt (wrong table name)
            GenerationResult(sql="SELECT * FROM users;", tokens_used=0),  # Second attempt (correct)
        ]

        mock_validator = MagicMock()
        # First call raises error, second call succeeds
        mock_validator.validate_or_raise.side_effect = [
            SQLParseError('relation "user" does not exist'),
            None,  # Success on second attempt
        ]

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(max_retries=3),
            validation_config=ValidationConfig(),
        )

        # Execute
        sql, validation_result, _tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=mock_schema,
            request_id="test-123",
        )

        # Verify
        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        assert mock_generator.generate_with_usage.call_count == 2
        assert mock_validator.validate_or_raise.call_count == 2

        # Verify retry included error feedback
        second_call = mock_generator.generate_with_usage.call_args_list[1]
        assert second_call.kwargs["previous_attempt"] == "SELECT * FROM user;"
        assert 'relation "user" does not exist' in second_call.kwargs["error_feedback"]

    @pytest.mark.asyncio
    async def test_generate_sql_fails_after_max_retries(self, mock_schema: DatabaseSchema) -> None:
        """Test failure after exhausting all retries."""
        # Setup mocks - all attempts fail validation
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="DELETE FROM users;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError(
            "DELETE statements are not allowed"
        )

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(max_retries=2),
            validation_config=ValidationConfig(),
        )

        # Execute and verify exception
        with pytest.raises(SecurityViolationError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Delete all users",
                schema=mock_schema,
                request_id="test-123",
            )

        assert "DELETE statements are not allowed" in str(exc_info.value)
        # Should attempt max_retries + 1 times (initial + retries)
        assert mock_generator.generate_with_usage.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_generate_sql_circuit_breaker_open(self, mock_schema: DatabaseSchema) -> None:
        """Test that open circuit breaker prevents SQL generation."""
        orchestrator = QueryOrchestrator(
            sql_generator=AsyncMock(),
            sql_validator=MagicMock(),
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(circuit_breaker_threshold=1),
            validation_config=ValidationConfig(),
        )

        # Manually open the circuit breaker
        orchestrator.circuit_breaker._state = CircuitState.OPEN
        orchestrator.circuit_breaker._failure_count = 5

        # Attempt should fail immediately
        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                request_id="test-123",
            )

        assert "temporarily unavailable" in str(exc_info.value).lower()
        assert "circuit breaker" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_sql_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors during generation."""
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.side_effect = RuntimeError("Unexpected error")

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=MagicMock(),
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(max_retries=1),
            validation_config=ValidationConfig(),
        )

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=mock_schema,
                request_id="test-123",
            )

        assert "unexpectedly" in str(exc_info.value).lower()
        assert orchestrator.circuit_breaker.failure_count == 1


class TestResultValidation:
    """Test result validation logic."""

    @pytest.mark.asyncio
    async def test_validate_results_success(self) -> None:
        """Test successful result validation."""
        mock_validator = AsyncMock()
        mock_validator.validate.return_value = ResultValidationResult(
            confidence=85,
            explanation="Results match the question well",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"test_db": MagicMock()},
            result_validator=mock_validator,
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=True),
        )

        confidence, tokens_used = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 85
        assert tokens_used == 0
        mock_validator.validate.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_results_disabled(self) -> None:
        """Test that validation is skipped when disabled."""
        mock_validator = AsyncMock()

        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"test_db": MagicMock()},
            result_validator=mock_validator,
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=False),
        )

        confidence, tokens_used = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100
        assert tokens_used == 0
        mock_validator.validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_results_failure_does_not_raise(self) -> None:
        """Test that validation failures don't raise exceptions."""
        mock_validator = AsyncMock()
        mock_validator.validate.side_effect = Exception("Validation failed")

        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"test_db": MagicMock()},
            result_validator=mock_validator,
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=True),
        )

        # Should not raise, returns default confidence
        confidence, tokens_used = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100
        assert tokens_used == 0


class TestExecuteQueryFlow:
    """Test complete query execution flow."""

    @pytest.fixture
    def mock_schema(self) -> DatabaseSchema:
        """Create mock database schema."""
        return DatabaseSchema(
            database_name="test_db",
            tables=[
                TableInfo(
                    schema_name="public",
                    table_name="users",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="integer",
                            is_nullable=False,
                            is_primary_key=True,
                        ),
                        ColumnInfo(
                            name="name",
                            data_type="varchar(255)",
                            is_nullable=False,
                        ),
                    ],
                )
            ],
            version="15.0",
        )

    @pytest.mark.asyncio
    async def test_execute_query_sql_only(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=SQL."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT * FROM users;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        assert response.generated_sql == "SELECT * FROM users;"
        assert response.validation is not None
        assert response.validation.is_valid is True
        assert response.data is None  # No execution for SQL-only
        assert response.error is None

    @pytest.mark.asyncio
    async def test_execute_query_with_results(self, mock_schema: DatabaseSchema) -> None:
        """Test executing query with return_type=RESULT."""
        # Setup mocks
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT id, name FROM users;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.execute.return_value = (
            [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
            ],
            2,  # total count
        )

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="Good results",
            suggestion=None,
            is_acceptable=True,
        )

        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": mock_executor},
            result_validator=mock_result_validator,
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=True),
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        assert response.generated_sql == "SELECT id, name FROM users;"
        assert response.data is not None
        assert response.data.row_count == 2
        assert len(response.data.rows) == 2
        assert response.data.columns == ["id", "name"]
        assert response.confidence == 90
        assert response.error is None

    @pytest.mark.asyncio
    async def test_execute_query_schema_not_cached(self) -> None:
        """Test loading schema when not in cache."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = None  # Not in cache
        mock_cache.load = AsyncMock(return_value=mock_schema)

        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT 1;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_pool = MagicMock()

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": mock_pool},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        # Execute
        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify schema was loaded
        mock_cache.load.assert_called_once_with("test_db", mock_pool)
        assert response.success is True

    @pytest.mark.asyncio
    async def test_execute_query_schema_load_fails(self) -> None:
        """Test handling of schema load failure."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.load = AsyncMock(side_effect=Exception("DB connection failed"))

        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        # Execute
        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "schema" in response.error.message.lower()
        assert response.generated_sql is None

    @pytest.mark.asyncio
    async def test_execute_query_validation_error(self) -> None:
        """Test handling of SQL validation errors."""
        mock_schema = DatabaseSchema(
            database_name="test_db",
            tables=[],
            version="15.0",
        )

        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="DELETE FROM users;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError("DELETE not allowed")

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(max_retries=1),
            validation_config=ValidationConfig(),
        )

        # Execute
        request = QueryRequest(
            question="Delete all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "DELETE not allowed" in response.error.message
        assert response.error.code == "security_violation"

    @pytest.mark.asyncio
    async def test_execute_query_execution_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of SQL execution errors."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT * FROM users;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        mock_executor = AsyncMock()
        mock_executor.execute.side_effect = DatabaseError("Query execution failed")

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": mock_executor},
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert "execution failed" in response.error.message.lower()
        assert response.error.code == "database_error"

    @pytest.mark.asyncio
    async def test_execute_query_unexpected_error(self, mock_schema: DatabaseSchema) -> None:
        """Test handling of unexpected errors."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.side_effect = RuntimeError("Unexpected error")

        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"test_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        # Execute
        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify error response
        assert response.success is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        assert "internal server error" in response.error.message.lower()

    @pytest.mark.asyncio
    async def test_execute_query_auto_select_database(self, mock_schema: DatabaseSchema) -> None:
        """Test auto-selecting database when only one available."""
        # Setup mocks
        mock_cache = MagicMock()
        mock_cache.get.return_value = mock_schema

        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT 1;", tokens_used=0
        )

        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"only_db": MagicMock()},
            result_validator=MagicMock(),
            schema_cache=mock_cache,
            pools={"only_db": MagicMock()},  # Only one database
            default_database="only_db",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(),
        )

        # Execute without specifying database
        request = QueryRequest(
            question="Test query",
            database=None,  # No database specified
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        # Verify
        assert response.success is True
        # Verify schema was fetched for auto-selected database
        mock_cache.get.assert_called_once_with("only_db")


class TestMultiDatabaseRouting:
    """The executor must follow the resolved database name (P4 fix)."""

    @pytest.mark.asyncio
    async def test_executor_follows_requested_database(self) -> None:
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT 1;", tokens_used=0
        )
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None
        mock_validator.blocked_tables = set()
        mock_validator.extract_tables.return_value = []

        executor_db1 = AsyncMock()
        executor_db2 = AsyncMock()
        executor_db2.execute.return_value = ([{"two": 2}], 1)

        mock_result_validator = AsyncMock()
        mock_result_validator.validate.return_value = ResultValidationResult(
            confidence=95, explanation="ok", suggestion=None, is_acceptable=True
        )

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"db1": executor_db1, "db2": executor_db2},
            result_validator=mock_result_validator,
            schema_cache=MagicMock(),
            pools={"db1": MagicMock(), "db2": MagicMock()},
            default_database="db1",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="db2", return_type=ReturnType.RESULT)
        )

        assert response.success is True
        executor_db2.execute.assert_awaited_once()
        executor_db1.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_database_rejected_with_available_list(self) -> None:
        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"db1": AsyncMock(), "db2": AsyncMock()},
            result_validator=AsyncMock(),
            schema_cache=MagicMock(),
            pools={"db1": MagicMock(), "db2": MagicMock()},
            default_database="db1",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="nope", return_type=ReturnType.RESULT)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_error"
        assert set(response.error.details["available_databases"]) == {"db1", "db2"}

    @pytest.mark.asyncio
    async def test_default_database_used_when_unspecified(self) -> None:
        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="SELECT 1;", tokens_used=0
        )
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.return_value = None
        mock_validator.blocked_tables = set()
        mock_validator.extract_tables.return_value = []

        executor_default = AsyncMock()
        executor_default.execute.return_value = ([{"one": 1}], 1)
        executor_other = AsyncMock()

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"db1": executor_default, "db2": executor_other},
            result_validator=AsyncMock(),
            schema_cache=MagicMock(),
            pools={"db1": MagicMock(), "db2": MagicMock()},
            default_database="db1",
            resilience_config=ResilienceConfig(),
            validation_config=ValidationConfig(enabled=False),
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database=None, return_type=ReturnType.RESULT)
        )

        assert response.success is True
        executor_default.execute.assert_awaited_once()
        executor_other.execute.assert_not_awaited()


class TestRateLimiting:
    """Requests are bounded by the query rate limiter (P8 fix)."""

    @pytest.mark.asyncio
    async def test_rate_limit_rejects_with_rate_limited(self) -> None:
        from pg_mcp.resilience.rate_limiter import MultiRateLimiter

        rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        # Exhaust the only query slot
        await rate_limiter.query_limiter.acquire()

        orchestrator = QueryOrchestrator(
            sql_generator=MagicMock(),
            sql_validator=MagicMock(),
            executors={"db1": AsyncMock()},
            result_validator=AsyncMock(),
            schema_cache=MagicMock(),
            pools={"db1": MagicMock()},
            default_database="db1",
            resilience_config=ResilienceConfig(rate_limit_timeout=0.1),
            validation_config=ValidationConfig(enabled=False),
            rate_limiter=rate_limiter,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="db1", return_type=ReturnType.RESULT)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"
        rate_limiter.query_limiter.release()

    @pytest.mark.asyncio
    async def test_llm_slot_rejection_maps_to_rate_limited(self) -> None:
        from pg_mcp.resilience.rate_limiter import MultiRateLimiter

        rate_limiter = MultiRateLimiter(query_limit=5, llm_limit=1)
        await rate_limiter.llm_limiter.acquire()

        orchestrator = QueryOrchestrator(
            sql_generator=AsyncMock(),
            sql_validator=MagicMock(),
            executors={"db1": AsyncMock()},
            result_validator=AsyncMock(),
            schema_cache=MagicMock(),
            pools={"db1": MagicMock()},
            default_database="db1",
            resilience_config=ResilienceConfig(rate_limit_timeout=0.1),
            validation_config=ValidationConfig(enabled=False),
            rate_limiter=rate_limiter,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="db1", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"
        rate_limiter.llm_limiter.release()


class TestRetryBackoff:
    """Retries wait retry_delay * backoff_factor**attempt between attempts (P10)."""

    @pytest.mark.asyncio
    async def test_backoff_delays_between_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", fake_sleep)

        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.side_effect = [
            GenerationResult(sql="SELECT * FROM user;", tokens_used=1),
            GenerationResult(sql="SELECT * FROM user2;", tokens_used=1),
            GenerationResult(sql="SELECT * FROM users;", tokens_used=1),
        ]
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = [
            SQLParseError('relation "user" does not exist'),
            SQLParseError('relation "user2" does not exist'),
            None,
        ]

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": AsyncMock()},
            result_validator=AsyncMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(max_retries=3, retry_delay=1.0, backoff_factor=2.0),
            validation_config=ValidationConfig(enabled=False),
        )

        sql, _validation, _tokens = await orchestrator._generate_sql_with_retry(
            question="Get users", schema=MagicMock(), request_id="test-backoff"
        )

        assert sql == "SELECT * FROM users;"
        assert delays == [1.0, 2.0]  # retry_delay * backoff_factor**attempt


class TestCircuitBreakerInjection:
    """A shared circuit breaker can be injected (P9 fix)."""

    @pytest.mark.asyncio
    async def test_injected_breaker_receives_failures(self) -> None:
        from pg_mcp.resilience.circuit_breaker import CircuitBreaker, CircuitState

        shared_breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

        mock_generator = AsyncMock()
        mock_generator.generate_with_usage.return_value = GenerationResult(
            sql="DELETE FROM users;", tokens_used=0
        )
        mock_validator = MagicMock()
        mock_validator.validate_or_raise.side_effect = SecurityViolationError("nope")

        orchestrator = QueryOrchestrator(
            sql_generator=mock_generator,
            sql_validator=mock_validator,
            executors={"test_db": AsyncMock()},
            result_validator=AsyncMock(),
            schema_cache=MagicMock(),
            pools={"test_db": MagicMock()},
            default_database="test_db",
            resilience_config=ResilienceConfig(max_retries=0),
            validation_config=ValidationConfig(enabled=False),
            circuit_breaker=shared_breaker,
        )

        assert shared_breaker.state == CircuitState.CLOSED
        with pytest.raises(SecurityViolationError):
            await orchestrator._generate_sql_with_retry(
                question="q", schema=MagicMock(), request_id="test-cb"
            )
        # The failure was recorded on the shared instance, not a private one
        assert shared_breaker.failure_count == 1
        assert shared_breaker.state == CircuitState.OPEN
        assert orchestrator.circuit_breaker is shared_breaker
