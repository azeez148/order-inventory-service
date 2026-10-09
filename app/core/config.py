"""Validated database configuration from the process environment."""

import os

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    database_url: SecretStr
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout: float = Field(default=10, gt=0, allow_inf_nan=False)

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
            }
        )
