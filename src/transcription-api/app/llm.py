from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol
from urllib import error, request

from data.repository import UserProfile


@dataclass(frozen=True)
class GeneratedProfile:
    summary: str
    interests: str
    communication_style: str
    persona_notes: str
    recent_updates: str


class LLMClient(Protocol):
    def summarize_session(self, transcript: str) -> str:
        ...

    def update_profile(
        self,
        *,
        username: str,
        existing_profile: UserProfile | None,
        existing_doc_text: str,
        transcript: str,
    ) -> GeneratedProfile:
        ...


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        provider_name: str = "openai",
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.api_key_env = api_key_env
        self.provider_name = provider_name
        self._client = None

    def summarize_session(self, transcript: str) -> str:
        content = self._chat(
            system=(
                "You summarize Discord voice-call conversations for the people who were there. "
                "The Summary length should be corresponding to the lenght of the conversation"
                "Return a Discord-ready summary with 4 to 7 short bullets. Cover the main topics, decisions, disagreements, notable jokes or moments, and any follow-up actions. "
                "Be specific and useful, but do not invent facts, do not include timestamps, and do not mention that this came from a transcript. "
                "Keep the whole answer under 1800 characters."
            ),
            user=f"Transcript:\n{transcript}",
        )
        return normalize_summary(content)

    def update_profile(
        self,
        *,
        username: str,
        existing_profile: UserProfile | None,
        existing_doc_text: str,
        transcript: str,
    ) -> GeneratedProfile:
        existing = asdict(existing_profile) if existing_profile else {}
        content = self._chat(
            system=(
                "You write lively, creative Discord user profiles from voice-call transcripts. "
                "Make the profile feel specific, colorful, and useful for understanding the person's vibe, while staying grounded in what they actually said. "
                "Do not list identifiers, usernames, Discord IDs, or generic account facts. "
                "Return only valid JSON with keys: summary, interests, communication_style, persona_notes, recent_updates."
            ),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing profile document text:\n{existing_doc_text or '<empty>'}\n\n"
                f"Latest session transcript:\n{transcript}"
            ),
            json_format=True,
        )
        return generated_profile_from_json(content)

    def _chat(self, *, system: str, user: str, json_format: bool = False) -> str:
        if not self.api_key:
            raise RuntimeError(f"{self.api_key_env} is required when LLM_PROVIDER={self.provider_name}")

        client = self._load_client()
        kwargs = {}
        if json_format:
            kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        choice = response.choices[0] if response.choices else None
        content = getattr(getattr(choice, "message", None), "content", None)
        if content:
            return str(content).strip()
        raise RuntimeError("OpenAI-compatible response did not include choices[0].message.content")

    def _load_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client


GroqClient = OpenAICompatibleClient


class OllamaClient:
    def __init__(self, *, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def summarize_session(self, transcript: str) -> str:
        content = self._chat(
            system=(
                "You summarize Discord voice-call conversations for the people who were there. "
                "Write in the same language as the conversation, using European Portuguese when the conversation is mostly Portuguese. "
                "Return a Discord-ready summary with 4 to 7 short bullets. Cover the main topics, decisions, disagreements, notable jokes or moments, and any follow-up actions. "
                "Be specific and useful, but do not invent facts, do not include timestamps, and do not mention that this came from a transcript. "
                "Keep the whole answer under 1800 characters."
            ),
            user=f"Transcript:\n{transcript}",
        )
        return normalize_summary(content)

    def update_profile(
        self,
        *,
        username: str,
        existing_profile: UserProfile | None,
        existing_doc_text: str,
        transcript: str,
    ) -> GeneratedProfile:
        existing = asdict(existing_profile) if existing_profile else {}
        content = self._chat(
            system=(
                "You write lively, creative Discord user profiles from voice-call transcripts. "
                "Make the profile feel specific, colorful, and useful for understanding the person's vibe, while staying grounded in what they actually said. "
                "Do not list identifiers, usernames, Discord IDs, or generic account facts. "
                "Return only valid JSON with keys: summary, interests, communication_style, persona_notes, recent_updates."
            ),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing profile document text:\n{existing_doc_text or '<empty>'}\n\n"
                f"Latest session transcript:\n{transcript}"
            ),
            json_format=True,
        )
        return generated_profile_from_json(content)

    def _chat(self, *, system: str, user: str, json_format: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if json_format:
            payload["format"] = "json"

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=180) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama HTTP {exc.code}: {raw_error}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Ollama is not reachable at {self.base_url}: {exc.reason}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama returned non-JSON response: {raw[:500]}") from exc

        message = data.get("message") or {}
        content = str(message.get("content", "")).strip()
        if not content:
            raise RuntimeError(f"Ollama response did not include message.content: {json.dumps(data)[:1000]}")
        return content


def generated_profile_from_json(raw: str) -> GeneratedProfile:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()

    data = json.loads(raw)
    return GeneratedProfile(
        summary=str(data.get("summary", "")).strip(),
        interests=str(data.get("interests", "")).strip(),
        communication_style=str(data.get("communication_style", "")).strip(),
        persona_notes=str(data.get("persona_notes", data.get("known_facts", ""))).strip(),
        recent_updates=str(data.get("recent_updates", "")).strip(),
    )


def normalize_summary(content: str) -> str:
    summary = "\n".join(line.strip() for line in content.splitlines() if line.strip())
    if not summary:
        return ""
    return summary[:1800].rstrip()
