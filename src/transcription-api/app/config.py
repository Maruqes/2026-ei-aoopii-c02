from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_UPLOAD_BYTES = 250 * 1024 * 1024


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    whisper_model: str = "base"
    whisper_device: str = "auto"
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    upload_tmp_dir: Path = Path(".tmp/uploads")
    recordings_dir: Path = Path("discord_bot/recordings")
    keep_uploads: bool = False
    llm_provider: str = "groq"
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:2b"
    profile_docs_provider: str = "local"
    local_profile_dir: Path = Path("profiles")

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            database_url = "postgresql://discord:discord@127.0.0.1:5432/discord_anthropologist"

        return cls(
            database_url=database_url,
            whisper_model=os.getenv("WHISPER_MODEL", "base"),
            whisper_device=os.getenv("WHISPER_DEVICE", "auto").strip().lower(),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))),
            upload_tmp_dir=Path(os.getenv("UPLOAD_TMP_DIR", ".tmp/uploads")),
            recordings_dir=Path(os.getenv("RECORDINGS_DIR", "discord_bot/recordings")),
            keep_uploads=env_bool("KEEP_UPLOADS", False),
            llm_provider=os.getenv("LLM_PROVIDER", "groq").strip().lower(),
            groq_api_key=os.getenv("GROQ_API_KEY"),
            groq_base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:2b"),
            profile_docs_provider=os.getenv("PROFILE_DOCS_PROVIDER", "local").strip().lower(),
            local_profile_dir=Path(os.getenv("LOCAL_PROFILE_DIR", "profiles")),
        )
