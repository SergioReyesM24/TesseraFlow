from pathlib import Path
from typing import Any

import bootstrap
from bootstrap import build_container
from config import (
    DEFAULT_AGENT_INSTRUCTIONS,
    DEFAULT_REALTIME_AGENT_INSTRUCTIONS,
    DEFAULT_WORKER_AGENT_INSTRUCTIONS,
    PROJECT_ENV_FILE,
    PROMPT_DIRECTORY,
    Settings,
)


def test_project_env_file_is_independent_of_working_directory() -> None:
    """Resolve the project dotenv file relative to the source tree."""
    assert PROJECT_ENV_FILE == Path(__file__).resolve().parents[1] / ".env"


def test_default_prompts_are_loaded_from_versioned_markdown_files() -> None:
    """Keep both agent prompts editable without embedding prose in Python settings."""
    assert PROMPT_DIRECTORY == Path(__file__).resolve().parents[1] / "src" / "prompts"
    assert (
        DEFAULT_AGENT_INSTRUCTIONS
        == (PROMPT_DIRECTORY / "interactive_agent.md").read_text(encoding="utf-8").strip()
    )
    assert (
        DEFAULT_WORKER_AGENT_INSTRUCTIONS
        == (PROMPT_DIRECTORY / "worker_agent.md").read_text(encoding="utf-8").strip()
    )
    assert (
        DEFAULT_REALTIME_AGENT_INSTRUCTIONS
        == (PROMPT_DIRECTORY / "realtime_agent.md").read_text(encoding="utf-8").strip()
    )
    assert "Do not ask the user" in DEFAULT_AGENT_INSTRUCTIONS
    assert "immediately call" in DEFAULT_AGENT_INSTRUCTIONS
    assert "Voy a consultarlo, dame un momento." in DEFAULT_AGENT_INSTRUCTIONS


def test_explicit_settings_can_override_markdown_prompts() -> None:
    """Preserve environment and constructor overrides for deployed configurations."""
    settings = Settings(
        agent_instructions="Interactive override",
        worker_agent_instructions="Worker override",
    )

    assert settings.agent_instructions == "Interactive override"
    assert settings.worker_agent_instructions == "Worker override"


def test_endpoint_and_worker_models_have_independent_provider_settings() -> None:
    """Configure text, realtime, and worker roles independently."""
    settings = Settings(
        text_agent_provider="openai",
        text_agent_model="text-model",
        realtime_agent_provider="gemini",
        realtime_agent_model="realtime-model",
        worker_provider="openai",
        gemini_api_key="gemini-key",
        openai_api_key="openai-key",
        worker_agent_model="worker-model",
    )

    assert settings.text_agent_provider == "openai"
    assert settings.text_agent_model == "text-model"
    assert settings.realtime_agent_provider == "gemini"
    assert settings.realtime_agent_model == "realtime-model"
    assert settings.worker_provider == "openai"
    assert settings.worker_agent_model == "worker-model"
    assert settings.gemini_api_key == "gemini-key"
    assert settings.openai_api_key == "openai-key"


def test_realtime_has_bounded_pcm_and_outbound_queues() -> None:
    """Validate realtime input and queued-memory boundaries without a flow switch."""
    settings = Settings(
        realtime_audio_max_chunk_bytes=3_200,
        realtime_outbound_max_audio_bytes=6_400,
    )

    assert settings.realtime_audio_max_chunk_bytes == 3_200
    assert settings.realtime_outbound_max_audio_bytes == 6_400


def test_removed_interactive_and_ambiguous_model_variables_are_ignored(
    monkeypatch: Any,
) -> None:
    """Apply the configuration break without aliases or legacy flow selection."""
    monkeypatch.setenv("INTERACTIVE_FLOW", "live_audio")
    monkeypatch.setenv("INTERACTIVE_PROVIDER", "legacy-provider")
    monkeypatch.setenv("INTERACTIVE_MODEL", "legacy-interactive")
    monkeypatch.setenv("OPENAI_MODEL", "legacy-openai")
    monkeypatch.setenv("GEMINI_LIVE_MODEL", "legacy-gemini")

    settings = Settings(_env_file=None)

    assert settings.text_agent_provider == "openai"
    assert settings.text_agent_model == "gpt-5-mini"
    assert settings.realtime_agent_provider == "gemini"
    assert settings.realtime_agent_model == "gemini-3.1-flash-live-preview"
    assert not hasattr(settings, "interactive_flow")


def test_legacy_interaction_poll_names_configure_reconciliation() -> None:
    """Accept deployed polling variables while exposing notification semantics."""
    settings = Settings(
        INTERACTION_COORDINATOR_POLL_SECONDS=7,
        INTERACTION_OUTPUT_POLL_SECONDS=9,
    )

    assert settings.interaction_coordinator_reconciliation_seconds == 7
    assert settings.interaction_output_reconciliation_seconds == 9


def test_legacy_a2a_poll_name_configures_job_reconciliation() -> None:
    """Accept the deployed A2A polling variable under notification semantics."""
    settings = Settings(A2A_WORKER_POLL_SECONDS=7)

    assert settings.a2a_worker_reconciliation_seconds == 7
    assert settings.a2a_worker_poll_seconds == 7


def test_blank_optional_dotenv_values_keep_provider_defaults(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Treat blank optional entries in the example configuration as unset values."""
    variable_names = (
        "OPENAI_BASE_URL",
        "GEMINI_LIVE_LANGUAGE_CODE",
    )
    for variable_name in variable_names:
        monkeypatch.delenv(variable_name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TEXT_AGENT_MODEL=\nREALTIME_AGENT_MODEL=\n"
        "WORKER_AGENT_MODEL=\nOPENAI_BASE_URL=\nGEMINI_LIVE_LANGUAGE_CODE=\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.text_agent_model == "gpt-5-mini"
    assert settings.realtime_agent_model == "gemini-3.1-flash-live-preview"
    assert settings.worker_agent_model == "gpt-5-mini"
    assert settings.openai_base_url is None
    assert settings.gemini_live_language_code is None


class FakePostgresPool:
    """Minimal pool lifecycle used by container composition tests."""

    def __init__(self) -> None:
        """Track whether graceful shutdown closes the pool."""
        self.closed = False

    async def close(self) -> None:
        """Record pool closure."""
        self.closed = True


async def test_container_exposes_only_the_configured_model_runtime(monkeypatch: Any) -> None:
    """Keep concrete provider clients behind the process-level model runtime."""
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
        text_agent_provider="openai",
        text_agent_model="interactive-model",
        realtime_agent_provider="gemini",
        openai_api_key="test-key",
        openai_base_url="https://example.openai.azure.com/openai/v1",
        openai_connect_timeout_seconds=15,
        postgres_url="postgresql://test",
    )

    container = await build_container(settings)

    try:
        assert container.model_runtime.text_agent_provider == "openai"
        assert container.model_runtime.realtime_agent_provider == "gemini"
        assert container.model_runtime.worker_provider == "openai"
        assert container.text_definition.model == "interactive-model"
        assert container.text_agent_service is container.model_runtime.text_agent_service
        assert not hasattr(container, "openai_client")
        assert not hasattr(container, "gemini_client")
    finally:
        await container.close()
    assert pool.closed is True
