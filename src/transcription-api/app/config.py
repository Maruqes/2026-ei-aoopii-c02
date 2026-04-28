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
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    upload_tmp_dir: Path = Path(".tmp/uploads")
    keep_uploads: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            database_url = "postgresql://discord:discord@127.0.0.1:5432/discord_anthropologist"

        return cls(
            database_url=database_url,
            whisper_model=os.getenv("WHISPER_MODEL", "base"),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))),
            upload_tmp_dir=Path(os.getenv("UPLOAD_TMP_DIR", ".tmp/uploads")),
            keep_uploads=env_bool("KEEP_UPLOADS", False),
        )
