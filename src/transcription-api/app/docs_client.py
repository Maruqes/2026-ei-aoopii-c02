from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .llm import GeneratedProfile


@dataclass(frozen=True)
class StoredDoc:
    doc_id: str | None
    url: str | None


class LocalMarkdownProfileClient:
    def __init__(self, *, profile_dir: Path):
        self.profile_dir = profile_dir

    def read_doc_text(self, doc_id: str | None) -> str:
        if not doc_id:
            return ""
        path = self._resolve_path(doc_id)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def upsert_profile_doc(
        self,
        *,
        doc_id: str | None,
        username: str,
        profile: GeneratedProfile,
    ) -> StoredDoc:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        filename = doc_id or f"{safe_filename(username)}.md"
        path = self.profile_dir / filename
        path.write_text(format_profile_markdown(username, profile), encoding="utf-8")
        return StoredDoc(doc_id=filename, url=str(path))

    def _resolve_path(self, doc_id: str) -> Path:
        path = Path(doc_id)
        if path.is_absolute():
            return path
        return self.profile_dir / doc_id


def format_profile_markdown(username: str, profile: GeneratedProfile) -> str:
    return (
        f"# {username}\n\n"
        f"## Summary\n{profile.summary}\n\n"
        f"## Interests\n{profile.interests}\n\n"
        f"## Communication Style\n{profile.communication_style}\n\n"
        f"## Persona Notes\n{profile.persona_notes}\n\n"
        f"## Recent Updates\n{profile.recent_updates}\n"
    )


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned or "profile"
