from __future__ import annotations

import asyncio
import copy
import re

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, model_validator


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
            words = re.findall(r"[^\W_]+", self.phrase.lower())
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


class AssistantQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def install_assistant_routes(service, *, get_repository, get_llm_client):
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
    async def question(request: AssistantQuestion, llm=Depends(get_llm_client)):
        text = " ".join(request.question.split())
        if not text:
            raise HTTPException(400, "Question is required")
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
                answer = await asyncio.to_thread(
                    bounded.answer_question, question=text, language="pt"
                )
        except TimeoutError:
            raise HTTPException(504, "Assistant timed out") from None
        return {"question": text, "answer": answer}
