from pydantic import Field, PostgresDsn

from app.core.settings.base import BaseContextSettings


class PostgresSettings(BaseContextSettings):
    DATABASE_HOST: str = "identity-postgres"
    DATABASE_PORT: int = Field(default=5432, ge=1, le=65535)
    DATABASE_USER: str = "andruha_identity"
    DATABASE_PASSWORD: str = "identity-local-only"
    DATABASE_NAME: str = "andruha_identity"
    RUN_MIGRATIONS: bool = False
    DATABASE_POOL_SIZE: int = Field(default=10, ge=1, le=100)
    DATABASE_MAX_OVERFLOW: int = Field(default=20, ge=0, le=100)
    DATABASE_POOL_TIMEOUT: float = Field(default=30.0, ge=0.1)
    DATABASE_POOL_RECYCLE: int = Field(default=1800, ge=1)

    @property
    def DATABASE_URL(self) -> str:
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.DATABASE_USER,
                password=self.DATABASE_PASSWORD,
                host=self.DATABASE_HOST,
                port=self.DATABASE_PORT,
                path=self.DATABASE_NAME,
            )
        )

    @property
    def url(self) -> str:
        return self.DATABASE_URL
