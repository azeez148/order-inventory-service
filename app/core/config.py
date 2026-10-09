"""Validated database and search configuration from the process environment."""

import os
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, validate_default=True)

    database_url: SecretStr
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout: float = Field(default=10, gt=0, allow_inf_nan=False)
    reporting_pool_size: int = Field(default=2, gt=0)
    reporting_pool_timeout: float = Field(default=5, gt=0, allow_inf_nan=False)
    reporting_statement_timeout_ms: int = Field(default=10000, gt=0, le=2147483647)
    search_provider: Literal["meilisearch"] = "meilisearch"
    meilisearch_url: AnyHttpUrl = AnyHttpUrl("http://meilisearch:7700")
    meilisearch_master_key: SecretStr = SecretStr("")
    meilisearch_index: str = Field(default="products", pattern=r"^[A-Za-z0-9_-]+$")
    search_http_timeout: float = Field(default=5, gt=0, allow_inf_nan=False)
    search_task_timeout: float = Field(default=30, gt=0, allow_inf_nan=False)
    outbox_enabled: bool = True
    outbox_poll_interval: float = Field(default=1, gt=0, allow_inf_nan=False)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except (ArgumentError, ValueError):
            raise ValueError("DATABASE_URL must be a valid database URL") from None
        if url.drivername != "postgresql+asyncpg" or not url.host or not url.database:
            raise ValueError(
                "DATABASE_URL must use postgresql+asyncpg with a host and database"
            )
        return value

    @model_validator(mode="after")
    def validate_reporting_capacity(self) -> "Settings":
        if self.reporting_pool_size >= self.db_pool_size + self.db_max_overflow:
            raise ValueError("Reporting pool must be smaller than total OLTP connection capacity")
        return self

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls.model_validate(
            {
                "database_url": os.environ.get("DATABASE_URL", ""),
                "db_pool_size": os.environ.get("DB_POOL_SIZE", "5"),
                "db_max_overflow": os.environ.get("DB_MAX_OVERFLOW", "5"),
                "db_pool_timeout": os.environ.get("DB_POOL_TIMEOUT", "10"),
                "reporting_pool_size": os.environ.get("REPORTING_POOL_SIZE", "2"),
                "reporting_pool_timeout": os.environ.get("REPORTING_POOL_TIMEOUT", "5"),
                "reporting_statement_timeout_ms": os.environ.get("REPORTING_STATEMENT_TIMEOUT_MS", "10000"),
                "search_provider": os.environ.get("SEARCH_PROVIDER", "meilisearch"),
                "meilisearch_url": os.environ.get("MEILISEARCH_URL", "http://meilisearch:7700"),
                "meilisearch_master_key": os.environ.get("MEILISEARCH_MASTER_KEY", ""),
                "meilisearch_index": os.environ.get("MEILISEARCH_INDEX", "products"),
                "search_http_timeout": os.environ.get("SEARCH_HTTP_TIMEOUT", "5"),
                "search_task_timeout": os.environ.get("SEARCH_TASK_TIMEOUT", "30"),
                "outbox_enabled": os.environ.get("OUTBOX_ENABLED", "true"),
                "outbox_poll_interval": os.environ.get("OUTBOX_POLL_INTERVAL", "1"),
            }
        )
