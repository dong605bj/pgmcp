"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.
"""

import json
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Shared dotenv settings so nested config classes also read the project .env file
_ENV_FILE_CONFIG = {
    "env_file": ".env",
    "env_file_encoding": "utf-8",
    "case_sensitive": False,
    "extra": "ignore",
}


def _parse_string_list(v: Any) -> Any:
    """Parse a string list from JSON, comma-separated text, or a native list.

    Shared by every ``list[str]`` config field so they all accept the same
    value shapes (JSON list, ``["a","b"]`` shell-sourced text, comma separated).

    Args:
        v: Raw value from env/file.

    Returns:
        list[str] | Any: Parsed list, or the original value when not a string.
    """
    if isinstance(v, str):
        # Try JSON format first (e.g. from .env with quoted list)
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (json.JSONDecodeError, ValueError):
            pass
        # Strip outer brackets left by shell sourcing of JSON-style values
        cleaned = v.strip().lstrip("[").rstrip("]").strip()
        return [item.strip().strip("'\"") for item in cleaned.split(",") if item.strip()]
    return v


class DatabaseConfig(BaseSettings):
    """PostgreSQL database connection configuration."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_", **_ENV_FILE_CONFIG)

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="postgres", description="Database name")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    # Per-database security profile overrides merged on top of the global
    # SecurityConfig (e.g. {"blocked_tables": ["secret.t"], "allowed_schemas": ["yancheng"]}).
    security_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-database SecurityConfig overrides",
    )

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


class OpenAIConfig(BaseSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_", **_ENV_FILE_CONFIG)

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible API base URL (explicit; e.g. http://gateway:3030/v1)",
    )
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(default=2000, ge=100, le=4096, description="Maximum tokens in response")
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Validate API key is not empty.

        Supports custom gateway tokens (e.g. bearer tokens from OpenAI-compatible
        LLM services), so no 'sk-' prefix format check is enforced.
        """
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        return v


class SecurityConfig(BaseSettings):
    """Security and access control configuration."""

    model_config = SettingsConfigDict(env_prefix="SECURITY_", **_ENV_FILE_CONFIG)

    allow_write_operations: bool = Field(
        default=False, description="Allow write operations (INSERT, UPDATE, DELETE)"
    )
    blocked_functions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    blocked_tables: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description='Blocked tables; entries may be "table" or "schema.table"',
    )
    blocked_columns: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description='Blocked columns; entries may be "column" or "table.column"',
    )
    allowed_schemas: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Schema allowlist for explicitly qualified tables; "
            "empty means no schema restriction"
        ),
    )
    allow_explain: bool = Field(
        default=False, description="Allow EXPLAIN statements (inner query is still validated)"
    )
    allow_explain_analyze: bool = Field(
        default=False,
        description="Allow EXPLAIN ANALYZE (executes the inner query; independent switch)",
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )

    @field_validator(
        "blocked_functions",
        "blocked_tables",
        "blocked_columns",
        "allowed_schemas",
        mode="before",
    )
    @classmethod
    def parse_string_list_field(cls, v: Any) -> Any:
        """Parse list-valued fields from JSON, comma-separated text, or list."""
        return _parse_string_list(v)


class ValidationConfig(BaseSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(env_prefix="VALIDATION_", **_ENV_FILE_CONFIG)

    max_question_length: int = Field(
        default=10000, ge=1, le=50000, description="Maximum question length in characters"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence for acceptable results"
    )


class CacheConfig(BaseSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(env_prefix="CACHE_", **_ENV_FILE_CONFIG)

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")


class ResilienceConfig(BaseSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(env_prefix="RESILIENCE_", **_ENV_FILE_CONFIG)

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0, ge=0.1, le=10.0, description="Initial retry delay in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    max_concurrent_queries: int = Field(
        default=10, ge=1, le=1000, description="Maximum concurrent database query requests"
    )
    max_concurrent_llm: int = Field(
        default=5, ge=1, le=1000, description="Maximum concurrent LLM API calls"
    )
    rate_limit_timeout: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description="Seconds to wait for a rate limiter slot before rejecting a request",
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )


class ObservabilityConfig(BaseSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_", **_ENV_FILE_CONFIG)

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="text", description="Log format")


class Settings(BaseSettings):
    """Main application settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )

    # Nested configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    databases: Annotated[list[DatabaseConfig], NoDecode] = Field(
        default_factory=list,
        description=(
            'Multiple databases as JSON: DATABASES=\'[{"name":"db1",...},{"name":"db2",...}]\'. '
            "When empty, falls back to the single [database] entry (backward compatible)."
        ),
    )
    default_database: str | None = Field(
        default=None,
        description="Database used when a request does not specify one (defaults to the first)",
    )
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @field_validator("databases", mode="before")
    @classmethod
    def parse_databases(cls, v: Any) -> Any:
        """Parse DATABASES from a JSON string (or pass through native lists)."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"DATABASES must be a JSON array of database configs: {e}") from e
        return v

    @model_validator(mode="after")
    def _normalize_databases(self) -> "Settings":
        """Merge single-database config into databases and validate names."""
        if not self.databases:
            self.databases = [self.database]

        names = [db.name for db in self.databases]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate database names in databases: {duplicates}")

        if self.default_database is None:
            self.default_database = names[0]
        if self.default_database not in names:
            raise ValueError(f"default_database '{self.default_database}' is not one of {names}")
        return self

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None
