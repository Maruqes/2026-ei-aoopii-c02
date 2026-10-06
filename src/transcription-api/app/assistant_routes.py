from __future__ import annotations

import asyncio
import copy
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from .agent import format_transcript
from .profile_updater import run_text_profile_sync


class AssistantChanges(BaseModel):
    enabled: bool | None = None
    phrase: str | None = Field(default=None, max_length=50)
    channel_id: str | None = None

    @model_validator(mode="after")
    def validate_changes(self):
        changes = self.model_dump(exclude_unset=True)
        if not changes or any(
            value is None for key, value in changes.items() if key != "channel_id"
        ):
            raise ValueError("Provide enabled, phrase or channel_id")
        if self.phrase is not None:
            words = re.findall(
                r"[^\W_]+", unicodedata.normalize("NFC", self.phrase).lower()
            )
            if not 2 <= len(words) <= 5 or any(
                not any(c.isalpha() for c in word) for word in words
            ):
                raise ValueError("Phrase must contain 2 to 5 useful words")
            self.phrase = " ".join(words)
        if self.channel_id is not None and not re.fullmatch(
            r"[0-9]{1,20}", self.channel_id
        ):
            raise ValueError("Invalid Discord channel ID")
        return self


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)
    interrupted: bool = False

    @model_validator(mode="after")
    def validate_message(self):
        if not self.content.strip() or (self.role == "user" and len(self.content) > 2000):
            raise ValueError("Invalid conversation message")
        if self.interrupted and self.role != "assistant":
            raise ValueError("Only assistant playback can be interrupted")
        return self


class AssistantQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: int | None = Field(default=None, gt=0)
    discord_id: str | None = Field(default=None, min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)
    history: list[AssistantMessage] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_identity(self):
        if self.session_id is not None or self.discord_id is not None:
            if (
                self.session_id is None
                or not self.discord_id
                or not self.discord_id.strip()
            ):
                raise ValueError("Provide both session_id and discord_id for memory")
        if self.history and self.session_id is None:
            raise ValueError("Conversation history requires a voice session and speaker")
        return self


def install_assistant_routes(
    service, *, get_repository, get_llm_client, get_docs_client
):
    @service.get("/v1/guilds/{guild_id}/assistant")
    def settings(guild_id: str, repository=Depends(get_repository)):
        return repository.assistant_settings(guild_id)

    @service.post("/v1/guilds/{guild_id}/assistant")
    def update(
        guild_id: str, request: AssistantChanges, repository=Depends(get_repository)
    ):
        return repository.update_assistant_settings(
            guild_id, request.model_dump(exclude_unset=True)
        )

    @service.post("/v1/assistant/question")
    async def question(
        request: AssistantQuestion,
        background_tasks: BackgroundTasks,
        llm=Depends(get_llm_client),
        repository=Depends(get_repository),
        docs=Depends(get_docs_client),
    ):
        text = " ".join(request.question.split())
        if not text:
            raise HTTPException(400, "Question is required")
        memory = ""
        inserted = None
        if request.session_id is not None:
            session = await asyncio.to_thread(
                repository.get_voice_session, request.session_id
            )
            if session is None:
                raise HTTPException(404, "Voice session not found")
            inserted = await asyncio.to_thread(
                repository.insert_assistant_question,
                session=session,
                discord_id=request.discord_id.strip(),
                username=(request.username or "").strip() or request.discord_id.strip(),
                display_name=request.display_name,
                question=text,
            )
            now = datetime.now(timezone.utc)
            messages = await asyncio.to_thread(
                repository.get_session_messages, session.id,
                since=now - timedelta(minutes=5), until=now, limit=501,
            )
            memory = "Current call, last 5 minutes (confirmed voice only):\n"
            if len(messages) > 500:
                memory += "[Partial coverage: newest 500 messages only]\n"
            memory += format_transcript(messages[-500:])
            background_tasks.add_task(
                run_text_profile_sync,
                repository=repository,
                llm=llm,
                docs=docs,
                user_id=inserted.user_id,
            )
        # Cached clients also serve summaries. Bound this request without mutating them.
        bounded = copy.copy(llm)
        bounded.max_retries = 0
        bounded.timeout_seconds = min(getattr(llm, "timeout_seconds", 30), 30)
        if hasattr(bounded, "_client"):
            bounded._client = (
                None  # Rebuild OpenAI transport with this request's deadline.
            )
        try:
            async with asyncio.timeout(30):
                kwargs = {"question": text, "language": "pt"}
                if memory:
                    kwargs["guild_context"] = memory
                    kwargs["current_speaker"] = (
                        f"{request.display_name or request.username or request.discord_id} "
                        f"[user={request.discord_id}]"
                    )
                if request.history:
                    kwargs["history"] = [m.model_dump() for m in request.history]
                answer = await asyncio.to_thread(bounded.answer_question, **kwargs)
        except TimeoutError:
            raise HTTPException(504, "Assistant timed out") from None
        if inserted is not None:
            await asyncio.to_thread(
                repository.save_assistant_answer, inserted.message_id, answer
            )
        return {"question": text, "answer": answer}
