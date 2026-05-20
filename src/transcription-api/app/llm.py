from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol
from urllib import request

from data.repository import UserProfile


@dataclass(frozen=True)
class GeneratedProfile:
    summary: str
    interests: str
    communication_style: str
    known_facts: str
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


class GrokClient:
    def __init__(self, *, api_key: str | None, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = None

    def summarize_session(self, transcript: str) -> str:
        content = self._complete(
            system=(
                "You summarize Discord voice-call transcripts. Return exactly one concise sentence. "
                "Do not mention that this came from a transcript."
            ),
            user=f"Transcript:\n{transcript}",
        )
        return " ".join(content.split())[:500]

    def update_profile(
        self,
        *,
        username: str,
        existing_profile: UserProfile | None,
        existing_doc_text: str,
        transcript: str,
    ) -> GeneratedProfile:
        existing = asdict(existing_profile) if existing_profile else {}
        content = self._complete(
            system=(
                "You update observational Discord user profiles from transcripts. "
                "Use only supported facts and careful observations. Do not diagnose, rank, or infer sensitive traits. "
                "Return strict JSON with keys: summary, interests, communication_style, known_facts, recent_updates."
            ),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing Google Doc text:\n{existing_doc_text or '<empty>'}\n\n"
                f"Latest session transcript:\n{transcript}"
            ),
        )
        return generated_profile_from_json(content)

    def _complete(self, *, system: str, user: str) -> str:
        if not self.api_key:
            raise RuntimeError("XAI_API_KEY is required for Grok profile generation")

        client = self._load_client()
        response = client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        output_text = getattr(response, "output_text", None)
        if output_text:
            return str(output_text).strip()
        raise RuntimeError("Grok response did not include output_text")

    def _load_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client


class OllamaClient:
    def __init__(self, *, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def summarize_session(self, transcript: str) -> str:
        content = self._chat(
            system=(
                "You summarize Discord voice-call transcripts. Return exactly one concise sentence. "
                "Do not mention that this came from a transcript."
            ),
            user=f"Transcript:\n{transcript}",
        )
        return " ".join(content.split())[:500]

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
                "You update observational Discord user profiles from transcripts. "
                "Use only supported facts and careful observations. Do not diagnose, rank, or infer sensitive traits. "
                "Return only valid JSON with keys: summary, interests, communication_style, known_facts, recent_updates."
            ),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing Google Doc text:\n{existing_doc_text or '<empty>'}\n\n"
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
        with request.urlopen(req, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))

        message = data.get("message") or {}
        content = str(message.get("content", "")).strip()
        if not content:
            raise RuntimeError("Ollama response did not include message.content")
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
        known_facts=str(data.get("known_facts", "")).strip(),
        recent_updates=str(data.get("recent_updates", "")).strip(),
    )
