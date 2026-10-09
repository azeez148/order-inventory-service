"""Validated database and search configuration from the process environment."""

import os
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, validate_default=True)

    database_url: SecretStr
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout: float = Field(default=10, gt=0, allow_inf_nan=False)
    search_provider: Literal["meilisearch"] = "meilisearch"
    meilisearch_url: AnyHttpUrl = AnyHttpUrl("http://meilisearch:7700")
    meilisearch_master_key: SecretStr = SecretStr("")
    meilisearch_index: str = Field(default="products", pattern=r"^[A-Za-z0-9_-]+$")
    search_http_timeout: float = Field(default=5, gt=0, allow_inf_nan=False)
    search_task_timeout: float = Field(default=30, gt=0, allow_inf_nan=False)

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

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls.model_validate(
            {
                "database_url": os.environ.get("DATABASE_URL", ""),
                "db_pool_size": os.environ.get("DB_POOL_SIZE", "5"),
                "db_max_overflow": os.environ.get("DB_MAX_OVERFLOW", "5"),
                "db_pool_timeout": os.environ.get("DB_POOL_TIMEOUT", "10"),
                "search_provider": os.environ.get("SEARCH_PROVIDER", "meilisearch"),
                "meilisearch_url": os.environ.get("MEILISEARCH_URL", "http://meilisearch:7700"),
                "meilisearch_master_key": os.environ.get("MEILISEARCH_MASTER_KEY", ""),
                "meilisearch_index": os.environ.get("MEILISEARCH_INDEX", "products"),
                "search_http_timeout": os.environ.get("SEARCH_HTTP_TIMEOUT", "5"),
                "search_task_timeout": os.environ.get("SEARCH_TASK_TIMEOUT", "30"),
            }
        )
