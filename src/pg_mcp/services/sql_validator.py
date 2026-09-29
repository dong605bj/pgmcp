"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.
"""

import re
from typing import ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {exp.Select, exp.Union, exp.Intersect, exp.Except}

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar = {
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.With,
        exp.Subquery,
    }

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool = False,
        allow_explain_analyze: bool = False,
        allowed_schemas: list[str] | None = None,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration containing blocked functions and settings.
            blocked_tables: Tables to block access to; entries may be "table"
                or "schema.table" for schema-qualified matching.
            blocked_columns: Columns to block access to; entries may be
                "column" or "table.column".
            allow_explain: Whether to allow EXPLAIN statements (the inner
                query is still fully validated).
            allow_explain_analyze: Whether to allow EXPLAIN ANALYZE, which
                actually executes the inner query.
            allowed_schemas: Schema allowlist; explicitly schema-qualified
                tables outside this list are rejected. Empty means no
                schema restriction.
        """
        self.config = config
        self.blocked_tables = {t.lower() for t in (blocked_tables or [])}
        self.blocked_columns = {c.lower() for c in (blocked_columns or [])}
        self.allowed_schemas = {s.lower() for s in (allowed_schemas or [])}
        self.allow_explain = allow_explain
        self.allow_explain_analyze = allow_explain_analyze

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None or isinstance(statement, type(None)):
            raise SQLParseError("No valid SQL statement found")

        # Handle EXPLAIN statements (parsed as Command in sqlglot)
        if isinstance(statement, exp.Command):
            # Check if it's an EXPLAIN command
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                if not self.allow_explain:
                    raise SecurityViolationError("EXPLAIN statements are not allowed")
                # EXPLAIN itself is read-only, but the inner query must pass
                # the same security checks (blocked functions/tables/columns).
                return self._validate_explain_inner(statement)
            else:
                # Other commands are not allowed
                raise SecurityViolationError(
                    f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted."
                )

        # Handle CTE (WITH) statements - extract the main query
        if isinstance(statement, exp.With):
            # WITH statements are allowed, but we need to validate the main query
            if statement.this:
                main_query = statement.this
            else:
                raise SQLParseError("WITH statement has no main query")
        else:
            main_query = statement

        # Perform security checks
        if error := self._check_statement_type(main_query):
            raise SecurityViolationError(error)

        # Deep scan: forbidden statement nodes anywhere in the AST (CTE bodies,
        # subqueries, nested expressions). A data-modifying statement hidden in
        # a CTE still executes on PostgreSQL, e.g.
        # "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x".
        if error := self._check_forbidden_statements(statement):
            raise SecurityViolationError(error)

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement):
            raise SecurityViolationError(error)

        if error := self._check_schema_allowlist(statement):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def _validate_explain_inner(self, statement: exp.Command) -> None:
        """Validate the query wrapped by EXPLAIN with the full security checks.

        sqlglot parses ``EXPLAIN ...`` as a Command whose inner SQL text lives
        in ``statement.expression.this``. ``EXPLAIN ANALYZE`` executes the
        inner query, so it requires the dedicated ``allow_explain_analyze``
        switch.

        Args:
            statement: The EXPLAIN Command node.

        Raises:
            SecurityViolationError: If ANALYZE is disallowed, the inner query
                cannot be parsed, or fails any security check.
            SQLParseError: If the EXPLAIN has no inner query.
        """
        expr = statement.expression
        inner_sql = ""
        if expr is not None:
            inner_sql = str(expr.this) if getattr(expr, "this", None) is not None else str(expr)

        analyze_match = re.match(
            r"^(ANALYZE|ANALYSE)\b\s*(.*)$", inner_sql.strip(), re.IGNORECASE | re.DOTALL
        )
        if analyze_match:
            if not self.allow_explain_analyze:
                raise SecurityViolationError(
                    "EXPLAIN ANALYZE is not allowed (it actually executes the inner query)"
                )
            inner_sql = analyze_match.group(2)

        if not inner_sql.strip():
            raise SQLParseError("EXPLAIN statement has no inner query to validate")

        # Re-parse the inner query and run the same checks as a top-level statement
        try:
            inner_statements = [
                s for s in sqlglot.parse(inner_sql, read="postgres") if s is not None
            ]
        except Exception as e:
            # Fail closed: an unparseable inner query must not pass EXPLAIN
            raise SecurityViolationError(f"EXPLAIN inner query failed validation: {e}") from e

        if len(inner_statements) != 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        inner_stmt = inner_statements[0]
        if isinstance(inner_stmt, exp.With):
            inner_stmt = inner_stmt.this or inner_stmt

        if error := self._check_statement_type(inner_stmt):
            raise SecurityViolationError(f"EXPLAIN inner query: {error}")

        if error := self._check_dangerous_functions(inner_statements[0]):
            raise SecurityViolationError(f"EXPLAIN inner query: {error}")

        if error := self._check_blocked_tables(inner_statements[0]):
            raise SecurityViolationError(f"EXPLAIN inner query: {error}")

        if error := self._check_blocked_columns(inner_statements[0]):
            raise SecurityViolationError(f"EXPLAIN inner query: {error}")

        if error := self._check_schema_allowlist(inner_statements[0]):
            raise SecurityViolationError(f"EXPLAIN inner query: {error}")

        if error := self._check_forbidden_statements(inner_statements[0]):
            raise SecurityViolationError(f"EXPLAIN inner query: {error}")

        if error := self._check_subquery_safety(inner_statements[0]):
            raise SecurityViolationError(f"EXPLAIN inner query: {error}")

    def _check_statement_type(self, statement: exp.Expression) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Check for forbidden statement types
        for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        # Ensure statement is an allowed type (SELECT or set operations)
        if not isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            stmt_type = type(statement).__name__
            return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

        return None

    def _check_dangerous_functions(self, statement: exp.Expression) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""

            if func_name in self.blocked_functions:
                return f"Function '{func_name}' is blocked for security reasons"

        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked tables.

        Entries may be a bare table name (matched in any schema) or a
        schema-qualified ``schema.table`` (matched exactly).

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        # Find all table references
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower() if table.name else ""
            schema_name = (table.db or "").lower()

            for entry in self.blocked_tables:
                if "." in entry:
                    entry_schema, _, entry_table = entry.partition(".")
                    if schema_name and schema_name == entry_schema and table_name == entry_table:
                        return f"Access to table '{schema_name}.{table_name}' is not allowed"
                elif table_name == entry:
                    return f"Access to table '{table_name}' is not allowed"

        return None

    def _check_blocked_columns(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked columns.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        # Find all column references
        for column in statement.find_all(exp.Column):
            column_name = column.name.lower() if column.name else ""

            # Check for exact match
            if column_name in self.blocked_columns:
                return f"Access to column '{column_name}' is not allowed"

            # Check for qualified column names (table.column)
            if column.table:
                qualified_name = f"{column.table.lower()}.{column_name}"
                if qualified_name in self.blocked_columns:
                    return f"Access to column '{qualified_name}' is not allowed"

        return None

    def _check_schema_allowlist(self, statement: exp.Expression) -> str | None:
        """Check that explicitly qualified tables use allowlisted schemas.

        Unqualified tables resolve through the (server-controlled) search_path
        and are not restricted here; only explicitly schema-qualified
        references are checked.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.allowed_schemas:
            return None

        for table in statement.find_all(exp.Table):
            schema_name = (table.db or "").lower()
            if schema_name and schema_name not in self.allowed_schemas:
                qualified = f"{schema_name}.{table.name.lower()}" if table.name else schema_name
                return (
                    f"Access to schema '{schema_name}' is not allowed "
                    f"('{qualified}'; allowed: {sorted(self.allowed_schemas)})"
                )

        return None

    def _check_forbidden_statements(self, statement: exp.Expression) -> str | None:
        """Deep-scan the AST for forbidden statement nodes at any nesting level.

        Covers CTE bodies, subqueries and any other nested statement — nodes
        that the top-level type check and subquery check do not reach.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if a forbidden node is found, None otherwise.
        """
        for node in statement.walk():
            for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                if isinstance(node, forbidden_type):
                    stmt_name = forbidden_type.__name__.upper()
                    return (
                        f"{stmt_name} statements are not allowed "
                        "(including inside CTEs or subqueries). "
                        "Only SELECT queries are permitted."
                    )
        return None

    def _check_subquery_safety(self, statement: exp.Expression) -> str | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all subqueries
        for subquery in statement.find_all(exp.Subquery):
            if subquery.this:
                inner_stmt = subquery.this

                # Check if the inner statement is a forbidden type
                for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                    if isinstance(inner_stmt, forbidden_type):
                        stmt_name = forbidden_type.__name__.upper()
                        return f"{stmt_name} statements in subqueries are not allowed"

                # Ensure it's a SELECT
                if not isinstance(inner_stmt, (exp.Select, exp.With)):
                    return "Subqueries must contain only SELECT statements"

        return None

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e
