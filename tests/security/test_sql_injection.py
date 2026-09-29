"""Security tests: SQL injection and bypass attempt matrix.

These tests verify that SQLValidator rejects injection patterns, multi-statement
payloads, and write operations hidden in CTEs or subqueries.
"""

import pytest

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError
from pg_mcp.services.sql_validator import SQLValidator


@pytest.fixture
def validator() -> SQLValidator:
    """Create a validator with default (strict) security config."""
    return SQLValidator(config=SecurityConfig())


class TestMultiStatementInjection:
    """Multi-statement payloads must be rejected."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM users; DROP TABLE users;--",
            "SELECT 1; SELECT 2",
            "SELECT * FROM t; INSERT INTO audit VALUES ('x')",
            "DROP TABLE users; SELECT 1",
        ],
    )
    def test_multiple_statements_rejected(self, validator: SQLValidator, sql: str) -> None:
        with pytest.raises(SecurityViolationError, match=r"[Mm]ultiple"):
            validator.validate_or_raise(sql)


class TestWriteInCteBypass:
    """Data-modifying statements hidden in CTEs still execute on PostgreSQL."""

    def test_delete_in_cte_rejected(self, validator: SQLValidator) -> None:
        sql = "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x"
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise(sql)

    def test_insert_in_cte_rejected(self, validator: SQLValidator) -> None:
        sql = "WITH x AS (INSERT INTO users VALUES (1) RETURNING *) SELECT * FROM x"
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise(sql)

    def test_update_in_cte_rejected(self, validator: SQLValidator) -> None:
        sql = "WITH x AS (UPDATE users SET active = false RETURNING *) SELECT * FROM x"
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise(sql)

    def test_drop_in_cte_rejected(self, validator: SQLValidator) -> None:
        sql = "WITH x AS (SELECT 1) SELECT * FROM x; DROP TABLE x"
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise(sql)

    def test_benign_cte_still_allowed(self, validator: SQLValidator) -> None:
        sql = "WITH totals AS (SELECT COUNT(*) AS n FROM users) SELECT n FROM totals"
        is_valid, error = validator.validate(sql)
        assert is_valid, error


class TestWriteInSubqueryBypass:
    """Write operations wrapped in subqueries must be rejected."""

    def test_delete_in_derived_table_rejected(self, validator: SQLValidator) -> None:
        sql = "SELECT * FROM (DELETE FROM users RETURNING *) d"
        with pytest.raises((SecurityViolationError, SQLParseError)):
            validator.validate_or_raise(sql)


class TestInjectionPayloads:
    """Classic injection payloads must not enable write access."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM users WHERE id = 1; DELETE FROM users",
            "SELECT * FROM users WHERE name = 'a';DROP TABLE users;--",
        ],
    )
    def test_stacked_payloads_rejected(self, validator: SQLValidator, sql: str) -> None:
        with pytest.raises((SecurityViolationError, SQLParseError)):
            validator.validate_or_raise(sql)

    def test_comment_truncated_select_still_read_only(self, validator: SQLValidator) -> None:
        """A commented-out suffix leaves a read-only statement — allowed."""
        sql = "SELECT id FROM users -- ; DROP TABLE users"
        is_valid, error = validator.validate(sql)
        assert is_valid, error

    def test_union_select_is_read_only_and_allowed(self, validator: SQLValidator) -> None:
        """UNION between two SELECTs stays within the read-only allowlist."""
        is_valid, error = validator.validate("SELECT id FROM users UNION SELECT id FROM admins")
        assert is_valid, error

    def test_unparseable_input_rejected(self, validator: SQLValidator) -> None:
        with pytest.raises(SQLParseError):
            validator.validate_or_raise("'))))((( SELECT !! ??")


class TestDangerousFunctions:
    """Dangerous functions are blocked anywhere in the statement."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT pg_sleep(100)",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT lo_import('/tmp/f')",
            "SELECT dblink('dbname=x', 'SELECT 1')",
            "SELECT * FROM t WHERE pg_sleep(1) IS NULL",
        ],
    )
    def test_blocked_functions_rejected(self, validator: SQLValidator, sql: str) -> None:
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise(sql)
