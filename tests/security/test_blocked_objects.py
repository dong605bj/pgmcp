"""Security tests: blocked tables/columns, schema allowlist, per-db profiles."""

import pytest

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError
from pg_mcp.services.sql_validator import SQLValidator


class TestBlockedTables:
    """Blocked table enforcement, including schema-qualified entries."""

    def test_bare_table_blocked_in_any_schema(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_tables=["users"])
        with pytest.raises(SecurityViolationError, match="users"):
            validator.validate_or_raise("SELECT * FROM users")
        with pytest.raises(SecurityViolationError, match="users"):
            validator.validate_or_raise("SELECT * FROM secret.users")

    def test_schema_qualified_entry_matches_exactly(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_tables=["yancheng.secret"])
        with pytest.raises(SecurityViolationError, match=r"yancheng\.secret"):
            validator.validate_or_raise("SELECT * FROM yancheng.secret")

    def test_same_table_name_in_other_schema_allowed(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_tables=["yancheng.secret"])
        is_valid, error = validator.validate("SELECT * FROM public.secret")
        assert is_valid, error

    def test_blocked_table_in_join_rejected(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_tables=["salaries"])
        with pytest.raises(SecurityViolationError, match="salaries"):
            validator.validate_or_raise(
                "SELECT u.name, s.amount FROM users u JOIN salaries s ON s.user_id = u.id"
            )

    def test_blocked_table_in_subquery_rejected(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_tables=["salaries"])
        with pytest.raises(SecurityViolationError, match="salaries"):
            validator.validate_or_raise(
                "SELECT * FROM users WHERE id IN (SELECT user_id FROM salaries)"
            )

    def test_empty_blocklist_allows_everything(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_tables=[])
        is_valid, error = validator.validate("SELECT * FROM any_table")
        assert is_valid, error


class TestBlockedColumns:
    """Blocked column enforcement."""

    def test_bare_column_blocked(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_columns=["password_hash"])
        with pytest.raises(SecurityViolationError, match="password_hash"):
            validator.validate_or_raise("SELECT password_hash FROM users")

    def test_qualified_column_blocked(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_columns=["users.password_hash"])
        with pytest.raises(SecurityViolationError, match=r"users\.password_hash"):
            validator.validate_or_raise("SELECT users.password_hash FROM users")

    def test_blocked_column_in_where_rejected(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), blocked_columns=["password_hash"])
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise("SELECT id FROM users WHERE password_hash = 'x'")


class TestSchemaAllowlist:
    """Schema allowlist enforcement on qualified references."""

    def test_non_allowlisted_schema_rejected(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), allowed_schemas=["public"])
        with pytest.raises(SecurityViolationError, match="schema"):
            validator.validate_or_raise("SELECT * FROM other_schema.t")

    def test_allowlisted_schema_allowed(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), allowed_schemas=["yancheng"])
        is_valid, error = validator.validate("SELECT * FROM yancheng.t_chat_message")
        assert is_valid, error

    def test_unqualified_table_not_restricted(self) -> None:
        """Unqualified tables resolve via the server-controlled search_path."""
        validator = SQLValidator(config=SecurityConfig(), allowed_schemas=["public"])
        is_valid, error = validator.validate("SELECT * FROM unqualified_table")
        assert is_valid, error

    def test_cte_qualifying_blocked_schema_rejected(self) -> None:
        validator = SQLValidator(config=SecurityConfig(), allowed_schemas=["public"])
        with pytest.raises(SecurityViolationError, match="schema"):
            validator.validate_or_raise("WITH x AS (SELECT * FROM hidden.t) SELECT * FROM x")


class TestPerDatabaseSecurityProfile:
    """Per-database SecurityConfig overrides merge onto the global profile."""

    def test_model_copy_applies_overrides(self) -> None:
        base = SecurityConfig()
        profile = base.model_copy(
            update={"blocked_tables": ["secret.payroll"], "allowed_schemas": ["yancheng"]}
        )
        assert base.blocked_tables == []
        assert profile.blocked_tables == ["secret.payroll"]
        assert profile.allowed_schemas == ["yancheng"]
        # Untouched fields inherit from the base profile
        assert profile.blocked_functions == base.blocked_functions
        assert profile.max_rows == base.max_rows

    def test_profile_enforced_by_validator(self) -> None:
        base = SecurityConfig()
        profile = base.model_copy(update={"blocked_tables": ["secret.payroll"]})
        validator = SQLValidator(config=profile, blocked_tables=profile.blocked_tables)
        with pytest.raises(SecurityViolationError):
            validator.validate_or_raise("SELECT * FROM secret.payroll")
