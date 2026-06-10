from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .llm import GeneratedProfile, LoreEvent


LORE_TIMELINE_HEADING = "## Lore Timeline"


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
        existing_doc_text = path.read_text(encoding="utf-8") if path.exists() else ""
        markdown = format_profile_markdown(username, profile, existing_doc_text=existing_doc_text)
        path.write_text(markdown, encoding="utf-8")
        return StoredDoc(doc_id=filename, url=str(path))

    def delete_doc(self, doc_id: str | None) -> bool:
        if not doc_id:
            return False
        path = self._resolve_path(doc_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _resolve_path(self, doc_id: str) -> Path:
        path = Path(doc_id)
        if path.is_absolute():
            return path
        return self.profile_dir / doc_id


def format_profile_markdown(
    username: str,
    profile: GeneratedProfile,
    *,
    existing_doc_text: str = "",
    observed_on: date | None = None,
) -> str:
    observed_on = observed_on or date.today()
    current_profile = (
        f"# {username}\n\n"
        f"> {profile.anthropologist_title or 'Field title pending'}\n\n"
        f"## Field Impression\n{profile.summary}\n\n"
        f"## Interests and Artifacts\n{profile.interests}\n\n"
        f"## Native Dialect\n{profile.communication_style}\n\n"
        f"## Social Role and Group Dynamics\n{profile.persona_notes}\n\n"
        f"## Current Pattern Notes\n{profile.recent_updates}\n\n"
    )
    timeline_entries = preserved_lore_timeline(existing_doc_text)
    new_entry = format_lore_entry(profile.lore_event, observed_on)
    timeline_parts = [part for part in (new_entry, timeline_entries) if part]
    timeline = "\n\n".join(timeline_parts)
    return f"{current_profile}{LORE_TIMELINE_HEADING}\n\n{timeline}\n".rstrip() + "\n"


def preserved_lore_timeline(markdown: str) -> str:
    match = re.search(rf"(?m)^{re.escape(LORE_TIMELINE_HEADING)}\s*$", markdown)
    if not match:
        return ""
    return markdown[match.end() :].strip("\n")


def format_lore_entry(lore_event: LoreEvent, observed_on: date) -> str:
    sections = [
        ("New Observations", lore_event.new_observations),
        ("Reinforced Patterns", lore_event.reinforced_patterns),
        ("Changed Interpretations", lore_event.changed_interpretations),
        ("Weakened or Retired Patterns", lore_event.weakened_or_retired_patterns),
    ]
    rendered_sections = []
    for heading, items in sections:
        if items:
            bullets = "\n".join(f"- {item}" for item in items)
            rendered_sections.append(f"**{heading}**\n{bullets}")
    if not rendered_sections:
        return ""
    title = lore_event.title or "Profile update"
    return f"### {observed_on.isoformat()} - {title}\n\n" + "\n\n".join(rendered_sections)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned or "profile"
