from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Environment-backed configuration validated once at application startup."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ENV_FILE, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TesseraFlow"
    openai_api_key: str = Field(default="", repr=False)
    openai_base_url: str | None = None
    openai_model: str = "gpt-5-mini"
    worker_agent_model: str | None = None
    openai_connect_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
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
    agent_instructions: str = (
        "You are the low-latency agent that talks directly to the user. Delegate work "
        "that requires operational tools to the worker agent. Never invent worker "
        "results. When a job is queued or running, explain that clearly and retain its "
        "job_id and thread_id; do not repeatedly poll it in the same turn. Check the job "
        "on a later user turn before claiming it is complete. Continue an existing worker "
        "thread when a follow-up depends on its prior tool results."
    )
    worker_agent_instructions: str = (
        "You are a persistent worker agent addressed by another agent as if it were a "
        "human user. Incoming messages use the tesseraflow.a2a JSON envelope; answer the "
        "request in its content field and preserve message_id only as protocol metadata. "
        "Use your operational tools when needed. Return a self-contained, factually precise "
        "report with the requested answer, relevant supporting details, assumptions, and "
        "additional context likely to help with follow-up questions. Remember that later "
        "messages belong to the same worker conversation."
    )
    a2a_worker_poll_seconds: float = Field(default=0.5, gt=0, le=60)
    a2a_job_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide validated settings instance."""
    return Settings()
