from pathlib import Path
from typing import Any

import bootstrap
from bootstrap import build_container
from config import PROJECT_ENV_FILE, Settings


def test_project_env_file_is_independent_of_working_directory() -> None:
    """Resolve the project dotenv file relative to the source tree."""
    assert PROJECT_ENV_FILE == Path(__file__).resolve().parents[1] / ".env"


class FakePostgresPool:
    """Minimal pool lifecycle used by container composition tests."""

    def __init__(self) -> None:
        """Track whether graceful shutdown closes the pool."""
        self.closed = False

    async def close(self) -> None:
        """Record pool closure."""
        self.closed = True


async def test_container_uses_configured_clients(monkeypatch: Any) -> None:
    """Route model requests through an OpenAI-compatible endpoint when configured."""
    pool = FakePostgresPool()

    async def create_pool(**kwargs: object) -> FakePostgresPool:
        """Return a fake pool without contacting PostgreSQL."""
        assert kwargs["dsn"] == "postgresql://test"
        return pool

    async def apply_migrations(value: object) -> None:
        """Confirm migrations receive the process-level pool."""
        assert value is pool

    monkeypatch.setattr(bootstrap.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(bootstrap, "apply_postgres_migrations", apply_migrations)
    settings = Settings(
        openai_api_key="test-key",
        openai_base_url="https://example.openai.azure.com/openai/v1",
        openai_connect_timeout_seconds=15,
        postgres_url="postgresql://test",
    )

    container = await build_container(settings)

    try:
        assert str(container.openai_client.base_url) == (
            "https://example.openai.azure.com/openai/v1/"
        )
        assert container.openai_client.timeout.connect == 15
    finally:
        await container.close()
    assert pool.closed is True
