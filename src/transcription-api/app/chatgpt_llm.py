from __future__ import annotations

from openai import APIConnectionError, APIStatusError, OpenAI

from .chatgpt_auth import RESOURCE, ChatGPTAuth, ChatGPTError, provider_error
from .llm import ConversationClient


class ChatGPTClient(ConversationClient):
    """Use the signed-in ChatGPT plan for the existing conversation features."""

    def __init__(
        self,
        *,
        auth: ChatGPTAuth,
        model: str,
        timeout_seconds: float = 90,
        context_chars: int = 24000,
        max_output_tokens: int = 2500,
    ):
        self.auth = auth
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.context_chars = context_chars
        # Used by shared prompt budgeting. The OAuth route forbids sending an
        # API max_output_tokens field, so it does not impose a server-side cap.
        self.max_output_tokens = max_output_tokens

    def list_models(self) -> list[str]:
        return [item["slug"] for item in self.auth.models()]

    def test_model(self) -> str:
        return self._chat(system="Reply briefly.", user="Ola!")

    def _chat(self, *, system: str, user: str, json_format: bool = False) -> str:
        if not self.model:
            raise ChatGPTError("Escolhe e testa um modelo em /chatgpt ou /models.")
        body = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": user}],
            "store": False,
            "stream": True,
        }
        if json_format:
            body["text"] = {"format": {"type": "json_object"}}
        completed = None
        pieces = []
        try:
            # Resolve the token on every request, including after refresh or
            # account switching. Never cache a client with an old bearer token.
            with OpenAI(
                api_key=self.auth.access_token(),
                base_url=RESOURCE,
                timeout=self.timeout_seconds,
                max_retries=0,
            ) as client:
                with client.responses.create(**body) as stream:
                    for event in stream:
                        if event.type == "response.output_text.delta":
                            pieces.append(event.delta)
                        elif event.type == "response.failed":
                            raise provider_error(
                                getattr(event.response.error, "code", "")
                            )
                        elif event.type == "response.incomplete":
                            raise ChatGPTError(
                                "A resposta ChatGPT ficou incompleta. O resultado não foi guardado."
                            )
                        elif event.type == "error":
                            raise provider_error(getattr(event, "code", ""))
                        elif event.type == "response.completed":
                            completed = event.response
        except APIStatusError as exc:
            error = (
                exc.body.get("error", exc.body) if isinstance(exc.body, dict) else {}
            )
            raise provider_error(
                error.get("code", "") if isinstance(error, dict) else ""
            ) from exc
        except APIConnectionError as exc:
            raise ChatGPTError(
                "Falha de ligação ao ChatGPT. Verifica a rede e tenta mais tarde."
            ) from exc
        if completed is None:
            raise ChatGPTError(
                "A ligação terminou antes de concluir a resposta ChatGPT."
            )
        text = (getattr(completed, "output_text", "") or "".join(pieces)).strip()
        if not text:
            raise ChatGPTError("ChatGPT não devolveu texto utilizável.")
        return text
