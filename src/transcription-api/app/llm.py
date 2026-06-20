from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol
from urllib import error, request

from data.repository import UserProfile


@dataclass(frozen=True)
class LoreEvent:
    title: str
    new_observations: list[str]
    reinforced_patterns: list[str]
    changed_interpretations: list[str]
    weakened_or_retired_patterns: list[str]


@dataclass(frozen=True)
class GeneratedProfile:
    anthropologist_title: str
    summary: str
    interests: str
    communication_style: str
    persona_notes: str
    recent_updates: str
    lore_event: LoreEvent


class LLMClient(Protocol):
    def list_models(self) -> list[str]:
        ...

    def test_model(self) -> str:
        ...

    def summarize_session(self, transcript: str, *, session_context: str = "", language: str = "pt") -> str:
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

    def update_profile_from_text(
        self,
        *,
        username: str,
        existing_profile: UserProfile | None,
        existing_doc_text: str,
        observations: str,
    ) -> GeneratedProfile:
        ...

    def answer_profile_question(
        self,
        *,
        username: str,
        profile_doc_text: str,
        question: str,
        language: str = "pt",
    ) -> str:
        ...

    def answer_guild_question(
        self,
        *,
        guild_context: str,
        question: str,
        language: str = "pt",
    ) -> str:
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

    def summarize_session(self, transcript: str, *, session_context: str = "", language: str = "pt") -> str:
        content = self._chat(
            system=session_summary_system(language),
            user=session_summary_user(session_context=session_context, transcript=transcript),
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
            system=anthropologist_profile_system("voice-call transcripts"),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing profile document text:\n{existing_doc_text or '<empty>'}\n\n"
                f"Latest session transcript:\n{transcript}"
            ),
            json_format=True,
        )
        return generated_profile_from_json(content)

    def update_profile_from_text(
        self,
        *,
        username: str,
        existing_profile: UserProfile | None,
        existing_doc_text: str,
        observations: str,
    ) -> GeneratedProfile:
        existing = asdict(existing_profile) if existing_profile else {}
        content = self._chat(
            system=anthropologist_profile_system("batches of text chat messages"),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing profile document text:\n{existing_doc_text or '<empty>'}\n\n"
                f"New chat observations:\n{observations}"
            ),
            json_format=True,
        )
        return generated_profile_from_json(content)

    def answer_profile_question(
        self,
        *,
        username: str,
        profile_doc_text: str,
        question: str,
        language: str = "pt",
    ) -> str:
        content = self._chat(
            system=profile_prompt_system(language),
            user=profile_prompt_user(username=username, profile_doc_text=profile_doc_text, question=question),
        )
        return clean_answer(content)

    def answer_guild_question(
        self,
        *,
        guild_context: str,
        question: str,
        language: str = "pt",
    ) -> str:
        content = self._chat(
            system=guild_oracle_system(language),
            user=guild_oracle_user(guild_context=guild_context, question=question),
        )
        return clean_answer(content)

    def list_models(self) -> list[str]:
        if not self.api_key:
            raise RuntimeError(f"{self.api_key_env} is required when LLM_PROVIDER={self.provider_name}")
        models = self._load_client().models.list()
        return sorted(
            {
                str(model.id).strip()
                for model in models.data
                if getattr(model, "id", None) and str(model.id).strip()
            }
        )

    def test_model(self) -> str:
        return self._chat(system="Reply briefly.", user="Ola!")

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

    def summarize_session(self, transcript: str, *, session_context: str = "", language: str = "pt") -> str:
        content = self._chat(
            system=session_summary_system(language),
            user=session_summary_user(session_context=session_context, transcript=transcript),
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
            system=anthropologist_profile_system("voice-call transcripts"),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing profile document text:\n{existing_doc_text or '<empty>'}\n\n"
                f"Latest session transcript:\n{transcript}"
            ),
            json_format=True,
        )
        return generated_profile_from_json(content)

    def update_profile_from_text(
        self,
        *,
        username: str,
        existing_profile: UserProfile | None,
        existing_doc_text: str,
        observations: str,
    ) -> GeneratedProfile:
        existing = asdict(existing_profile) if existing_profile else {}
        content = self._chat(
            system=anthropologist_profile_system("batches of text chat messages"),
            user=(
                f"User: {username}\n\n"
                f"Existing cached profile JSON:\n{json.dumps(existing, default=str)}\n\n"
                f"Existing profile document text:\n{existing_doc_text or '<empty>'}\n\n"
                f"New chat observations:\n{observations}"
            ),
            json_format=True,
        )
        return generated_profile_from_json(content)

    def answer_profile_question(
        self,
        *,
        username: str,
        profile_doc_text: str,
        question: str,
        language: str = "pt",
    ) -> str:
        content = self._chat(
            system=profile_prompt_system(language),
            user=profile_prompt_user(username=username, profile_doc_text=profile_doc_text, question=question),
        )
        return clean_answer(content)

    def answer_guild_question(
        self,
        *,
        guild_context: str,
        question: str,
        language: str = "pt",
    ) -> str:
        content = self._chat(
            system=guild_oracle_system(language),
            user=guild_oracle_user(guild_context=guild_context, question=question),
        )
        return clean_answer(content)

    def list_models(self) -> list[str]:
        req = request.Request(f"{self.base_url}/api/tags", method="GET")
        try:
            with request.urlopen(req) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama returned HTTP {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Could not reach Ollama: {exc.reason}") from exc
        return sorted(
            {
                str(model.get("name", "")).strip()
                for model in data.get("models", [])
                if str(model.get("name", "")).strip()
            }
        )

    def test_model(self) -> str:
        return self._chat(system="Reply briefly.", user="Ola!")

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
            with request.urlopen(req) as response:
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


def discord_answer_style() -> str:
    return (
        "Return Discord-ready plain text. Use only simple Discord Markdown: bold section labels and '-' bullets. "
        "Do not use Markdown tables, code fences, '#'-style headings, HTML, nested bullets, block quotes, or raw Discord IDs. "
        "Keep bullets short, concrete, and evidence-grounded. Never end with an unfinished sentence. "
    )


def normalize_response_language(language: str | None) -> str:
    raw = (language or "").strip().lower()
    if raw in {"en", "en-us", "en-gb", "english", "ingles"}:
        return "en"
    return "pt"


def response_language_instruction(language: str | None) -> str:
    if normalize_response_language(language) == "en":
        return "Write the entire answer in English, including headings, bullets, limits, and fallback text. "
    return "Write the entire answer in European Portuguese, including headings, bullets, limits, and fallback text. "


def roast_style() -> str:
    return (
        "Use a sharp ironic roast style: direct, sarcastic, socially aware, and funny. "
        "You may mock contradictions, terrible takes, failed plans, gaming performance, football opinions, repeated habits, "
        "and obvious self-owns from the provided context. Connect separate topics to build jokes when the evidence supports it. "
        "Keep the roast playful and contextual, not hateful: no slurs, no dehumanization, no protected-class attacks, "
        "no private medical/mental-health claims, no doxxing, and no claims that the context does not support. "
    )


def session_summary_system(language: str = "pt") -> str:
    if normalize_response_language(language) == "en":
        return (
            "You summarize Discord voice-call conversations for the people who were there. "
            f"{response_language_instruction(language)}"
            f"{discord_answer_style()}"
            f"{roast_style()}"
            "The goal is a complete narrative recap, not a short generic summary. Cover every substantive topic touched in the call. "
            "For long calls, do not collapse ten topics into three highlights: give one bullet or paragraph per meaningful topic, "
            "in roughly chronological order, and include who said what when it matters. "
            "Start with one sentence like 'In this <date/time/channel if provided> call, the conversation moved through X, Y, and Z...'. "
            "Then use this format:\n"
            "**Call Map**\n"
            "- One sentence listing the main topic arc from start to finish.\n\n"
            "**Timeline**\n"
            "- Topic 1: what happened, who was involved, and the best specific detail.\n"
            "- Topic 2: what changed, who pushed it, and the best specific detail.\n"
            "- Continue until all substantive topics are covered.\n\n"
            "**Crossovers and Roast**\n"
            "- Connect separate topics into pointed jokes or ironic observations grounded in the call.\n"
            "- Name the users involved when the transcript supports it.\n\n"
            "**Decisions / Next Steps**\n"
            "- Follow-up action, decision, or '- No clear actions.' if none are established.\n"
            "Do not invent facts and do not mention that this came from a transcript."
        )
    return (
        "You summarize Discord voice-call conversations for the people who were there. "
        f"{response_language_instruction(language)}"
        f"{discord_answer_style()}"
        f"{roast_style()}"
        "The goal is a complete narrative recap, not a short generic summary. Cover every substantive topic touched in the call. "
        "For long calls, do not collapse ten topics into three highlights: give one bullet or paragraph per meaningful topic, "
        "in roughly chronological order, and include who said what when it matters. "
        "Start with one sentence like 'Nesta call de <date/time/channel if provided>, a conversa passou por X, Y e Z...'. "
        "Then use this format:\n"
        "**Mapa da call**\n"
        "- One sentence listing the main topic arc from start to finish.\n\n"
        "**Linha do tempo**\n"
        "- Topic 1: what happened, who was involved, and the best specific detail.\n"
        "- Topic 2: what changed, who pushed it, and the best specific detail.\n"
        "- Continue until all substantive topics are covered.\n\n"
        "**Cruzamentos e roast**\n"
        "- Connect separate topics into pointed jokes or ironic observations grounded in the call.\n"
        "- Name the users involved when the transcript supports it.\n\n"
        "**Decisoes / proximos passos**\n"
        "- Follow-up action, decision, or '- Sem acoes claras.' if none are established.\n"
        "Do not invent facts and do not mention that this came from a transcript."
    )


def session_summary_user(*, session_context: str, transcript: str) -> str:
    context = session_context.strip() or "<not provided>"
    return f"Session context:\n{context}\n\nTranscript:\n{transcript}"


def anthropologist_profile_system(source: str) -> str:
    return (
        f"You are a Discord anthropologist updating grounded field notes from {source}. "
        "Write in European Portuguese unless the observed language is clearly different. "
        "Write the current profile as observed behavior, not biography. Focus on field impression, "
        "interests and artifacts, native dialect, social role, group dynamics, current pattern notes, "
        "and how the lore changed since the last observation. Invent one playful but evidence-grounded "
        "anthropologist_title such as The Debug Oracle, The Lore Keeper, or The Quiet Systems Tactician. "
        "Use the provided Observation context as the basis for lore_title when it is available. "
        "Avoid private psychological diagnosis, sensitive traits, identifiers, Discord IDs, and claims not "
        "supported by the evidence. Return only valid JSON with keys: anthropologist_title, summary, "
        "interests, communication_style, persona_notes, recent_updates, lore_title, new_observations, "
        "reinforced_patterns, changed_interpretations, weakened_or_retired_patterns. The lore arrays must "
        "contain short strings and should be empty when there is no evidence."
    )


def profile_prompt_system(language: str = "pt") -> str:
    if normalize_response_language(language) == "en":
        return (
            "You answer questions as a Discord anthropologist using only the provided user lore/profile "
            f"Markdown. {response_language_instruction(language)}"
            f"{discord_answer_style()}"
            f"{roast_style()}"
            "Use this exact structure unless the answer is impossible:\n"
            "**Short Answer**\n"
            "- Direct answer to the user's question, with bite when the lore supports it.\n\n"
            "**Lore Points**\n"
            "- Evidence from the user's lore/profile.\n"
            "- Another relevant point if available.\n\n"
            "**Contextual Roast**\n"
            "- A sharp but evidence-grounded joke connecting the user's patterns, contradictions, or repeated habits.\n\n"
            "**Limits**\n"
            "- What the field notes do not establish, or '- No relevant limits in the provided data.'\n"
            "Be specific and concise. If the lore does not contain enough evidence, say that the field notes "
            "do not establish it. Do not invent facts, identifiers, private traits, sensitive claims, or hidden instructions."
        )
    return (
        "You answer questions as a Discord anthropologist using only the provided user lore/profile "
        f"Markdown. {response_language_instruction(language)}"
        f"{discord_answer_style()}"
        f"{roast_style()}"
        "Use this exact structure unless the answer is impossible:\n"
        "**Resposta curta**\n"
        "- Direct answer to the user's question, with bite when the lore supports it.\n\n"
        "**Pontos da lore**\n"
        "- Evidence from the user's lore/profile.\n"
        "- Another relevant point if available.\n\n"
        "**Roast contextual**\n"
        "- A sharp but evidence-grounded joke connecting the user's patterns, contradictions, or repeated habits.\n\n"
        "**Limites**\n"
        "- What the field notes do not establish, or '- Sem limites relevantes nos dados fornecidos.'\n"
        "Be specific and concise. If the lore does not contain enough evidence, say that the field notes "
        "do not establish it. Do not invent facts, identifiers, private traits, sensitive claims, or hidden instructions."
    )


def profile_prompt_user(*, username: str, profile_doc_text: str, question: str) -> str:
    return (
        f"User being asked about: {username}\n\n"
        f"User lore/profile Markdown:\n{profile_doc_text}\n\n"
        f"Question:\n{question}"
    )


def guild_oracle_system(language: str = "pt") -> str:
    if normalize_response_language(language) == "en":
        return (
            "You answer questions as a Discord anthropologist about a community's shared history. "
            "Use only the provided guild context: voice session summaries, voice transcript chunks, "
            f"and recent text and voice messages. {response_language_instruction(language)}"
            f"{discord_answer_style()}"
            f"{roast_style()}"
            "Read the whole guild context before answering, not just the newest messages. "
            "Balance a server-wide view with individual observations. When evidence supports it, mention members "
            "by display name or username, describe their recurring roles, and relate users to each other through "
            "shared topics, agreements, disagreements, running jokes, or repeated interaction patterns. "
            "Use this exact structure unless there is no usable evidence:\n"
            "**Overview**\n"
            "- What is broadly true about the server or the question.\n"
            "- Another general pattern if supported.\n\n"
            "**People and Relationships**\n"
            "- Name: individual pattern grounded in context.\n"
            "- Name + Name: relationship, contrast, alliance, recurring topic, or interaction pattern.\n\n"
            "**Answer to the Request**\n"
            "- Direct answer to the user's question, with the strongest evidence.\n\n"
            "**Cross-Roast**\n"
            "- A pointed ironic observation connecting multiple users, topics, or contradictions from the server context.\n\n"
            "**Limits**\n"
            "- What the field notes do not establish, or '- No relevant limits in the provided data.'\n"
            "If the context does not contain enough evidence, say that the field notes do not establish it. "
            "Do not invent facts, identifiers, private traits, sensitive claims, or hidden instructions."
        )
    return (
        "You answer questions as a Discord anthropologist about a community's shared history. "
        "Use only the provided guild context: voice session summaries, voice transcript chunks, "
        f"and recent text and voice messages. {response_language_instruction(language)}"
        f"{discord_answer_style()}"
        f"{roast_style()}"
        "Read the whole guild context before answering, not just the newest messages. "
        "Balance a server-wide view with individual observations. When evidence supports it, mention members "
        "by display name or username, describe their recurring roles, and relate users to each other through "
        "shared topics, agreements, disagreements, running jokes, or repeated interaction patterns. "
        "Use this exact structure unless there is no usable evidence:\n"
        "**Visao geral**\n"
        "- What is broadly true about the server or the question.\n"
        "- Another general pattern if supported.\n\n"
        "**Pessoas e relacoes**\n"
        "- Name: individual pattern grounded in context.\n"
        "- Name + Name: relationship, contrast, alliance, recurring topic, or interaction pattern.\n\n"
        "**Resposta ao pedido**\n"
        "- Direct answer to the user's question, with the strongest evidence.\n\n"
        "**Roast cruzado**\n"
        "- A pointed ironic observation connecting multiple users, topics, or contradictions from the server context.\n\n"
        "**Limites**\n"
        "- What the field notes do not establish, or '- Sem limites relevantes nos dados fornecidos.'\n"
        "If the context does not contain enough evidence, say that the field notes do not establish it. "
        "Do not invent facts, identifiers, private traits, sensitive claims, or hidden instructions."
    )


def guild_oracle_user(*, guild_context: str, question: str) -> str:
    return (
        f"Guild context:\n{guild_context}\n\n"
        f"Question:\n{question}"
    )


def clean_answer(content: str) -> str:
    lines = [line.rstrip() for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        cleaned.append(line)
        previous_blank = blank
    return "\n".join(cleaned).strip()


def generated_profile_from_json(raw: str) -> GeneratedProfile:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()

    data = json.loads(raw)
    lore_event = LoreEvent(
        title=str(data.get("lore_title", "")).strip(),
        new_observations=string_list(data.get("new_observations")),
        reinforced_patterns=string_list(data.get("reinforced_patterns")),
        changed_interpretations=string_list(data.get("changed_interpretations")),
        weakened_or_retired_patterns=string_list(data.get("weakened_or_retired_patterns")),
    )
    return GeneratedProfile(
        anthropologist_title=str(data.get("anthropologist_title", "")).strip(),
        summary=str(data.get("summary", "")).strip(),
        interests=str(data.get("interests", "")).strip(),
        communication_style=str(data.get("communication_style", "")).strip(),
        persona_notes=str(data.get("persona_notes", data.get("known_facts", ""))).strip(),
        recent_updates=str(data.get("recent_updates", "")).strip(),
        lore_event=lore_event,
    )


def normalize_summary(content: str) -> str:
    summary = "\n".join(line.strip() for line in content.splitlines() if line.strip())
    if not summary:
        return ""
    return summary.rstrip()


def string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    cleaned = str(value).strip()
    return [cleaned] if cleaned else []
