from __future__ import annotations

import asyncio
from contextlib import closing

import pytest
from app import assistant_routes, main
from app.assistant_routes import AssistantChanges
from app.config import Settings
from app.llm import ConversationClient
from app.realtime import final_words
from fastapi.testclient import TestClient
from pydantic import ValidationError


@pytest.mark.parametrize(
    "phrase", ["", "bot", "!!!", "Hey 123", "a b c d e f", "x" * 51]
)
def test_invalid_phrase(phrase):
    with pytest.raises(ValidationError):
        AssistantChanges(phrase=phrase)


def test_settings_boundary_and_normalization():
    assert AssistantChanges(phrase="  HEY, Bot! ").phrase == "hey bot"
    assert AssistantChanges(phrase="Olá, Amigo!").phrase == "olá amigo"
    assert AssistantChanges(phrase="Ola\u0301, Macaco!").phrase == "olá macaco"
    for changes in ({}, {"enabled": None}, {"phrase": None}, {"channel_id": "invalid"}):
        with pytest.raises(ValidationError):
            AssistantChanges(**changes)


def test_general_answer_uses_shared_transport_without_history():
    class Client(ConversationClient):
        def _chat(self, **kwargs):
            self.input = kwargs
            return "\r\nResposta.\n\n\nFim. "

    client = Client()
    assert (
        client.answer_question(question="O que é polimorfismo?") == "Resposta.\n\nFim."
    )
    assert client.input["user"] == "O que é polimorfismo?"
    assert "European Portuguese" in client.input["system"]
    assert "cannot execute actions" in client.input["system"]
    assert "Guild context:" not in client.input["user"]


def test_word_positions_preserve_punctuation():
    assert final_words(
        [
            {
                "type": "word",
                "start_time": 1,
                "end_time": 2,
                "alternatives": [{"content": "Bot"}],
            },
            {"type": "punctuation", "alternatives": [{"content": ","}]},
            {
                "type": "word",
                "start_time": 2,
                "end_time": 3,
                "alternatives": [{"content": "Explica"}],
            },
        ]
    ) == [
        {"text": "Bot,", "start": 1, "end": 2},
        {"text": "Explica", "start": 2, "end": 3},
    ]


def test_settings_persist_atomically_and_preserve_streaming(repository):
    defaults = repository.assistant_settings("guild")
    assert defaults == {
        "enabled": True,
        "phrase": "Olá macaco",
        "channel_id": None,
        "revision": 0,
    }
    assert repository.streaming_preference("guild", False) is False
    saved = repository.update_assistant_settings(
        "guild", {"phrase": "olá bot", "channel_id": "123"}
    )
    assert saved["revision"] == 1
    assert repository.streaming_preference("guild", False) is False
    assert repository.streaming_preference("guild", True) is True
    repository.set_streaming_preference("guild", True)
    repository.update_assistant_settings("guild", {"enabled": False})
    reloaded = type(repository)(repository.database_url)
    assert reloaded.assistant_settings("guild") == {
        "enabled": False,
        "phrase": "olá bot",
        "channel_id": "123",
        "revision": 2,
    }
    assert reloaded.streaming_preference("guild", False) is True
    assert reloaded.assistant_settings("other") == defaults


class LLM:
    timeout_seconds = 90
    calls = []

    def answer_question(self, **kwargs):
        self.calls.append((kwargs, self.timeout_seconds, self.max_retries))
        return "Resposta."


def client_for(repository, tmp_path, llm):
    app = main.create_app()
    settings = Settings(database_url=repository.database_url, recordings_dir=tmp_path)
    app.dependency_overrides[main.get_repository] = lambda: repository
    app.dependency_overrides[main.get_settings] = lambda: settings
    app.dependency_overrides[main.get_llm_client] = lambda: llm
    return closing(TestClient(app))


def test_settings_api_and_question_without_server_history(repository, tmp_path):
    llm = LLM()
    llm.calls = []
    with client_for(repository, tmp_path, llm) as client:
        response = client.post(
            "/v1/guilds/g/assistant", json={"phrase": "Hey, BOT!", "channel_id": "123"}
        )
        assert response.status_code == 200
        assert client.get("/v1/guilds/g/assistant").json() == response.json()
        assert response.json()["phrase"] == "hey bot"
        assert (
            client.post("/v1/guilds/g/assistant", json={"phrase": "bot"}).status_code
            == 422
        )
        assert (
            client.post("/v1/assistant/question", json={"question": "   "}).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/assistant/question", json={"question": "x" * 2001}
            ).status_code
            == 422
        )
        response = client.post(
            "/v1/assistant/question", json={"question": " Explica  polimorfismo? "}
        )
        assert response.json() == {
            "question": "Explica polimorfismo?",
            "answer": "Resposta.",
        }
    assert llm.calls == [
        ({"question": "Explica polimorfismo?", "language": "pt"}, 30, 0)
    ]
    assert llm.timeout_seconds == 90


def test_question_api_timeout(repository, tmp_path, monkeypatch):
    llm = LLM()

    async def timed_out(*args, **kwargs):
        raise TimeoutError()

    # Isolate this module's worker invocation from FastAPI's repository workers.
    monkeypatch.setattr(assistant_routes, "asyncio", replace_asyncio(timed_out))
    with client_for(repository, tmp_path, llm) as client:
        assert (
            client.post(
                "/v1/assistant/question", json={"question": "Pergunta?"}
            ).status_code
            == 504
        )


def replace_asyncio(to_thread):
    from types import SimpleNamespace

    return SimpleNamespace(to_thread=to_thread, timeout=asyncio.timeout)


def test_phrase_upgrade_preserves_custom_settings_and_later_changes(repository):
    from data.apply_migrations import apply_migrations
    from data.repository import connect

    repository.set_streaming_preference("old", True)
    repository.update_assistant_settings("old", {"phrase": "hey bot"})
    custom = repository.update_assistant_settings("custom", {"phrase": "Olá amigo"})
    # Simulate the schema default and persisted settings of the previous version.
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE guild_transcription_settings ALTER COLUMN assistant_phrase SET DEFAULT 'Hey Bot'"
            )
        conn.commit()
    apply_migrations(repository.database_url)
    assert repository.assistant_settings("old")["phrase"] == "Olá macaco"
    assert repository.assistant_settings("old")["revision"] == 2
    assert repository.assistant_settings("custom") == custom
    assert repository.streaming_preference("old", False) is True
    repository.set_streaming_preference("new", False)
    assert repository.assistant_settings("new")["phrase"] == "Olá macaco"
    # Replaying migrations must not undo an explicit choice made after upgrading.
    saved = repository.update_assistant_settings("old", {"phrase": "Hey Bot"})
    apply_migrations(repository.database_url)
    assert repository.assistant_settings("old") == saved
