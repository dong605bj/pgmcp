"""Security tests: EXPLAIN policy enforcement.

EXPLAIN bypasses execution of the inner query, but ANALYZE does execute it.
The validator therefore: (a) gates EXPLAIN itself behind ``allow_explain``,
(b) gates ``EXPLAIN ANALYZE`` behind ``allow_explain_analyze``, and
(c) runs the inner query through the full security check set.
"""

import pytest

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError
from pg_mcp.services.sql_validator import SQLValidator


class TestExplainGate:
    """EXPLAIN itself is off by default."""

    def test_explain_rejected_by_default(self) -> None:
        validator = SQLValidator(config=SecurityConfig())
        with pytest.raises(SecurityViolationError, match=r"(?i)explain"):
            validator.validate_or_raise("EXPLAIN SELECT * FROM users")

    def test_explain_allowed_when_enabled(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), allow_explain=True)
        is_valid, error = validator.validate("EXPLAIN SELECT * FROM users")
        assert is_valid, error

    def test_other_commands_rejected_even_with_explain_enabled(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), allow_explain=True)
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise("VACUUM users")


class TestExplainAnalyzeGate:
    """EXPLAIN ANALYZE has an independent switch."""

    def test_analyze_requires_switch(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), allow_explain=True)
        with pytest.raises(SecurityViolationError, match=r"(?i)analyze"):
            validator.validate_or_raise("EXPLAIN ANALYZE SELECT * FROM users")

    def test_analyze_allowed_with_switch(self) -> None:
        validator = SQLValidator(
            config=SecurityConfig(), allow_explain=True, allow_explain_analyze=True
        )
        is_valid, error = validator.validate("EXPLAIN ANALYZE SELECT * FROM users")
        assert is_valid, error


class TestExplainInnerQueryValidation:
    """The inner query runs through the full security check set."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        return SQLValidator(config=SecurityConfig(), allow_explain=True)

    def test_blocked_function_inner_rejected(self, validator: SQLValidator) -> None:
        with pytest.raises(SecurityViolationError, match="pg_sleep"):
            validator.validate_or_raise("EXPLAIN SELECT pg_sleep(10)")

    def test_write_inner_rejected(self, validator: SQLValidator) -> None:
        with pytest.raises(SecurityViolationError, match=r"(?i)delete"):
            validator.validate_or_raise("EXPLAIN DELETE FROM users")

    def test_cte_write_inner_rejected(self, validator: SQLValidator) -> None:
        sql = "EXPLAIN WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x"
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise(sql)

    def test_blocked_table_inner_rejected(self) -> None:
        validator = SQLValidator(
            config=SecurityConfig(), allow_explain=True, blocked_tables=["salaries"]
        )
        with pytest.raises(SecurityViolationError, match="salaries"):
            validator.validate_or_raise("EXPLAIN SELECT * FROM salaries")

    def test_blocked_schema_inner_rejected(self) -> None:
        validator = SQLValidator(
            config=SecurityConfig(), allow_explain=True, allowed_schemas=["public"]
        )
        with pytest.raises(SecurityViolationError, match="schema"):
            validator.validate_or_raise("EXPLAIN SELECT * FROM hidden.t")

    def test_unparseable_inner_fails_closed(self, validator: SQLValidator) -> None:
        """An inner query we cannot parse must not pass EXPLAIN."""
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise("EXPLAIN SOME UNKNOWN COMMAND THING")
