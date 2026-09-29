"""Unit tests for server assembly (lifespan) and the MCP query tool."""

import asyncio
from unittest.mock import MagicMock

import pytest

import pg_mcp.server as server_module
from pg_mcp.models.query import ErrorInfo, QueryResponse, QueryResult
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.server import query


@pytest.fixture(autouse=True)
def restore_server_globals():
    """Save/restore server module globals across tests."""
    saved = (server_module._settings, server_module._pools, server_module._orchestrator)
    yield
    server_module._settings, server_module._pools, server_module._orchestrator = saved


class FakeSchemaCache:
    """Minimal schema cache double for lifespan assembly."""

    def __init__(self, config) -> None:
        self.config = config
        self.loaded: list[str] = []

    async def load(self, database_name: str, pool) -> DatabaseSchema:
        self.loaded.append(database_name)
        return DatabaseSchema(database_name=database_name, tables=[], version="15.0")

    async def stop_auto_refresh(self) -> None:
        return None


class StubOrchestrator:
    """Orchestrator double returning a canned response."""

    def __init__(self, response: QueryResponse) -> None:
        self.response = response
        self.request = None

    async def execute_query(self, request):
        self.request = request
        return self.response


class TestLifespanAssembly:
    """Lifespan wires multi-database pools, executors and shared resilience."""

    @pytest.mark.asyncio
    async def test_multi_database_assembly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "DATABASES",
            '[{"name":"db1"},{"name":"db2","security_overrides":{"max_rows":11}}]',
        )
        monkeypatch.setenv("OBSERVABILITY_METRICS_ENABLED", "false")

        pools = {"db1": MagicMock(name="pool1"), "db2": MagicMock(name="pool2")}

        async def fake_create_pools(configs):
            assert [c.name for c in configs] == ["db1", "db2"]
            return pools

        monkeypatch.setattr(server_module, "create_pools", fake_create_pools)
        monkeypatch.setattr(server_module, "SchemaCache", FakeSchemaCache)

        async with server_module.lifespan(server_module.mcp):
            settings = server_module._settings
            assert settings is not None
            assert settings.default_database == "db1"

            orchestrator = server_module._orchestrator
            assert set(orchestrator.executors) == {"db1", "db2"}
            assert orchestrator.default_database == "db1"
            assert orchestrator.pools is pools

            # Security validator built from configuration (not hardcoded None)
            assert orchestrator.sql_validator.blocked_tables == set(
                settings.security.blocked_tables
            )
            assert orchestrator.sql_validator.allow_explain == settings.security.allow_explain

            # Per-database security profile applied to the executor
            assert orchestrator.executors["db2"].security_config.max_rows == 11
            assert orchestrator.executors["db1"].security_config.max_rows == (
                settings.security.max_rows
            )

            # Shared resilience components are configured from settings
            assert orchestrator.rate_limiter.query_limiter.max_concurrent == (
                settings.resilience.max_concurrent_queries
            )
            assert orchestrator.rate_limiter.llm_limiter.max_concurrent == (
                settings.resilience.max_concurrent_llm
            )

            # Schema cache loaded for every database
            assert set(server_module._schema_cache.loaded) == {"db1", "db2"}


class TestQueryTool:
    """MCP query tool parameter handling and response contract."""

    def test_not_initialized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_module, "_orchestrator", None)
        result = asyncio.run(query(question="SELECT 1", return_type="result"))
        assert result["success"] is False
        assert result["error"]["code"] == "SERVER_NOT_INITIALIZED"
        assert result["tokens_used"] == 0

    def test_invalid_return_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            server_module, "_orchestrator", StubOrchestrator(QueryResponse(success=True))
        )
        result = asyncio.run(query(question="q", return_type="bogus"))
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert result["tokens_used"] == 0

    def test_empty_question_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            server_module, "_orchestrator", StubOrchestrator(QueryResponse(success=True))
        )
        result = asyncio.run(query(question="   ", return_type="result"))
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_REQUEST"

    def test_success_response_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tokens_used is always present; request_id flows through."""
        response = QueryResponse(
            success=True,
            generated_sql="SELECT 1;",
            data=QueryResult(columns=["x"], rows=[{"x": 1}], row_count=1, execution_time_ms=1.0),
            confidence=95,
            tokens_used=None,  # orchestrator did not report tokens
            request_id="req-123",
        )
        monkeypatch.setattr(server_module, "_orchestrator", StubOrchestrator(response))

        result = asyncio.run(query(question="how many?", return_type="result"))

        assert result["success"] is True
        assert "tokens_used" in result  # contract: key always present
        assert result["tokens_used"] == 0
        assert result["request_id"] == "req-123"

    def test_error_response_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = QueryResponse(
            success=False,
            error=ErrorInfo(code="database_error", message="boom"),
            confidence=0,
            tokens_used=None,
        )
        monkeypatch.setattr(server_module, "_orchestrator", StubOrchestrator(response))

        result = asyncio.run(query(question="q", return_type="result"))

        assert result["success"] is False
        assert "tokens_used" in result
        assert result["tokens_used"] == 0
        assert result["error"]["code"] == "database_error"
