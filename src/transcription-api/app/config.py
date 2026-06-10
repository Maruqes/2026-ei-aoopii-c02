from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _whisper_timeout_default() -> float:
    raw = os.getenv("WHISPER_TIMEOUT", "30m").strip()
    if not raw:
        return 1800
    try:
        seconds = float(raw)
        return seconds if seconds > 0 else 1800
    except ValueError:
        pass
    multipliers = {"s": 1, "m": 60, "h": 3600}
    unit = raw[-1].lower()
    if unit in multipliers:
        try:
            value = float(raw[:-1])
            return value * multipliers[unit]
        except ValueError:
            pass
    return 1800


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


@dataclass(frozen=True)
class Settings:
    database_url: str
    whisper_model: str = "large-v3"
    whisper_device: str = "auto"
    whisper_language: str = "pt"
    whisper_beam_size: int = 10
    whisper_initial_prompt: str = ""
    whisper_carry_initial_prompt: bool = False
    whisper_condition_on_previous_text: bool = False
    whisper_hallucination_silence_threshold: float = 2.0
    whisper_max_no_speech_prob: float = 0.8
    whisper_timeout_seconds: float = 1800
    upload_tmp_dir: Path = Path(".tmp/uploads")
    recordings_dir: Path = Path("discord_bot/recordings")
    keep_uploads: bool = False
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:2b"
    profile_docs_provider: str = "local"
    local_profile_dir: Path = Path("profiles")
    text_profile_sync_enabled: bool = True
    text_profile_sync_interval_hours: int = 12

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            database_url = "postgresql://discord:discord@127.0.0.1:5432/discord_anthropologist"

        return cls(
            database_url=database_url,
            whisper_model=os.getenv("WHISPER_MODEL", "large-v3"),
            whisper_device=os.getenv("WHISPER_DEVICE", "auto").strip().lower(),
            whisper_language=env_str("WHISPER_LANGUAGE", "pt"),
            whisper_beam_size=env_int("WHISPER_BEAM_SIZE", 10),
            whisper_initial_prompt=env_str("WHISPER_INITIAL_PROMPT"),
            whisper_carry_initial_prompt=env_bool("WHISPER_CARRY_INITIAL_PROMPT", False),
            whisper_condition_on_previous_text=env_bool("WHISPER_CONDITION_ON_PREVIOUS_TEXT", False),
            whisper_hallucination_silence_threshold=env_float(
                "WHISPER_HALLUCINATION_SILENCE_THRESHOLD",
                2.0,
            ),
            whisper_max_no_speech_prob=env_float("WHISPER_MAX_NO_SPEECH_PROB", 0.8),
            whisper_timeout_seconds=env_float("WHISPER_TIMEOUT_SECONDS", _whisper_timeout_default()),
            upload_tmp_dir=Path(os.getenv("UPLOAD_TMP_DIR", ".tmp/uploads")),
            recordings_dir=Path(os.getenv("RECORDINGS_DIR", "discord_bot/recordings")),
            keep_uploads=env_bool("KEEP_UPLOADS", False),
            llm_provider=env_str("LLM_PROVIDER", "openai").lower(),
            openai_api_key=env_str("OPENAI_API_KEY", env_str("GROQ_API_KEY")),
            openai_base_url=env_str(
                "OPENAI_BASE_URL",
                env_str("GROQ_BASE_URL", "https://api.openai.com/v1"),
            ),
            openai_model=env_str("OPENAI_MODEL", env_str("GROQ_MODEL", "gpt-4o-mini")),
            groq_api_key=env_str("GROQ_API_KEY"),
            groq_base_url=env_str("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            groq_model=env_str("GROQ_MODEL", "llama-3.3-70b-versatile"),
            ollama_base_url=env_str("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_model=env_str("OLLAMA_MODEL", "qwen3.5:2b"),
            profile_docs_provider=env_str("PROFILE_DOCS_PROVIDER", "local").lower(),
            local_profile_dir=Path(env_str("LOCAL_PROFILE_DIR", "profiles")),
            text_profile_sync_enabled=env_bool("TEXT_PROFILE_SYNC_ENABLED", True),
            text_profile_sync_interval_hours=env_int("TEXT_PROFILE_SYNC_INTERVAL_HOURS", 12),
        )
