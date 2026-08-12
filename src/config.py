from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
PROMPT_DIRECTORY = Path(__file__).resolve().parent / "prompts"


def load_prompt(filename: str) -> str:
    """Load one required default prompt independently from the process directory."""
    return PROMPT_DIRECTORY.joinpath(filename).read_text(encoding="utf-8").strip()


DEFAULT_AGENT_INSTRUCTIONS = load_prompt("interactive_agent.md")
DEFAULT_REALTIME_AGENT_INSTRUCTIONS = load_prompt("realtime_agent.md")
DEFAULT_WORKER_AGENT_INSTRUCTIONS = load_prompt("worker_agent.md")


class ModelRateTierSettings(BaseModel):
    """Input-size tier for models whose rates change with context length."""

    min_input_tokens: int = Field(ge=1)
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cached_input: Decimal | None = Field(default=None, ge=0)
    cached_input_audio: Decimal | None = Field(default=None, ge=0)
    input_audio: Decimal | None = Field(default=None, ge=0)
    output_audio: Decimal | None = Field(default=None, ge=0)


class ModelRateSettings(BaseModel):
    """Environment-validated price card per million tokens."""

    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cached_input: Decimal | None = Field(default=None, ge=0)
    cached_input_audio: Decimal | None = Field(default=None, ge=0)
    input_audio: Decimal | None = Field(default=None, ge=0)
    output_audio: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=1, max_length=12)
    tiers: list[ModelRateTierSettings] = Field(default_factory=list)


def default_model_pricing() -> dict[str, ModelRateSettings]:
    """Return € reference rates reviewed on 29-07-2026 for project models."""
    return {
        "gpt-5-mini": ModelRateSettings(
            input=Decimal("0.219684"),
            cached_input=Decimal("0.021968"),
            output=Decimal("1.757469"),
            currency="€",
        ),
        "gpt-5.4": ModelRateSettings(
            input=Decimal("2.193945"),
            cached_input=Decimal("0.219394"),
            output=Decimal("13.163668"),
            currency="€",
            tiers=[
                ModelRateTierSettings(
                    min_input_tokens=272_001,
                    input=Decimal("4.387889"),
                    cached_input=Decimal("0.438789"),
                    output=Decimal("19.745502"),
                )
            ],
        ),
        "gemini-3.1-flash-live-preview": ModelRateSettings(
            input=Decimal("0.659051"),
            input_audio=Decimal("2.636204"),
            output=Decimal("3.954306"),
            output_audio=Decimal("10.544815"),
            currency="€",
        ),
    }


class Settings(BaseSettings):
    """Environment-backed configuration validated once at application startup."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ENV_FILE, ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "TesseraFlow"
    text_agent_provider: str = "openai"
    text_agent_model: str = "gpt-5-mini"
    realtime_agent_provider: str = "gemini"
    realtime_agent_model: str = "gemini-3.1-flash-live-preview"
    worker_provider: str = "openai"
    openai_api_key: str = Field(default="", repr=False)
    openai_base_url: str | None = None
    openai_realtime_voice_name: str = "marin"
    openai_realtime_transcription_model: str = "gpt-4o-mini-transcribe"
    openai_realtime_language_code: str | None = None
    openai_realtime_reasoning_effort: str | None = None
    worker_agent_model: str = "gpt-5-mini"
    model_pricing: dict[str, ModelRateSettings] = Field(default_factory=default_model_pricing)
    openai_connect_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    gemini_api_key: str = Field(default="", repr=False)
    gemini_live_api_version: str = "v1beta"
    gemini_live_voice_name: str = "Zephyr"
    gemini_live_language_code: str | None = None
    realtime_audio_max_chunk_bytes: int = Field(default=32_768, ge=2, le=1_048_576)
    realtime_session_max_seconds: float = Field(default=1_800.0, ge=10, le=14_400)
    realtime_outbound_max_messages: int = Field(default=128, ge=1, le=10_000)
    realtime_outbound_max_audio_bytes: int = Field(default=131_072, ge=2, le=16_777_216)
    realtime_outbound_enqueue_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    realtime_resumption_max_attempts: int = Field(default=3, ge=0, le=20)
    realtime_resumption_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    realtime_proactive_turn_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    realtime_command_reconciliation_seconds: float = Field(default=5.0, gt=0, le=60)
    log_level: str = "INFO"
    log_json: bool = False
    max_tool_rounds: int = Field(default=8, ge=1, le=50)
    postgres_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/tesseraflow",
        repr=False,
    )
    postgres_pool_min_size: int = Field(default=1, ge=1, le=100)
    postgres_pool_max_size: int = Field(default=10, ge=1, le=100)
    postgres_command_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    redis_url: str = "redis://localhost:6379/0"
    conversation_ttl_seconds: int = Field(default=604_800, ge=60)
    conversation_max_messages: int = Field(default=100, ge=2, le=10_000)
    conversation_max_characters: int = Field(default=200_000, ge=2)
    conversation_max_bytes: int = Field(default=512_000, ge=256)
    agent_instructions: str = DEFAULT_AGENT_INSTRUCTIONS
    realtime_agent_instructions: str = DEFAULT_REALTIME_AGENT_INSTRUCTIONS
    worker_agent_instructions: str = DEFAULT_WORKER_AGENT_INSTRUCTIONS
    a2a_worker_reconciliation_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        validation_alias=AliasChoices(
            "a2a_worker_reconciliation_seconds",
            "A2A_WORKER_RECONCILIATION_SECONDS",
            "A2A_WORKER_POLL_SECONDS",
        ),
    )
    a2a_job_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    interaction_coordinator_reconciliation_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        validation_alias=AliasChoices(
            "INTERACTION_COORDINATOR_RECONCILIATION_SECONDS",
            "INTERACTION_COORDINATOR_POLL_SECONDS",
        ),
    )
    interaction_output_reconciliation_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        validation_alias=AliasChoices(
            "INTERACTION_OUTPUT_RECONCILIATION_SECONDS",
            "INTERACTION_OUTPUT_POLL_SECONDS",
        ),
    )
    interaction_command_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    interaction_max_pending_commands: int = Field(default=16, ge=1, le=1000)
    interaction_coordinator_workers: int = Field(default=4, ge=1, le=100)

    @property
    def a2a_worker_poll_seconds(self) -> float:
        """Expose the legacy name for callers migrating to reconciliation semantics."""
        return self.a2a_worker_reconciliation_seconds


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide validated settings instance."""
    return Settings()
