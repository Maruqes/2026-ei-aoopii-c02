from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from app import assistant_routes, main
from app.assistant_routes import AssistantChanges
from app.config import Settings
from app.docs_client import LocalMarkdownProfileClient
from app.llm import ConversationClient, GeneratedProfile, LoreEvent
from app.profile_updater import run_text_profile_sync
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
    assert "Do not narrate your sources or processing" in client.input["system"]
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


@pytest.mark.parametrize("repeats", [1, 300])
def test_general_answer_reads_memory_without_turning_bot_words_into_facts(repeats):
    class Client(ConversationClient):
        context_chars = 4000

        def _chat(self, **kwargs):
            assert (
                len(kwargs["system"]) + len(kwargs["user"]) + 128 <= self.context_chars
            )
            if kwargs["user"].startswith('{"slice":'):
                return "Ana: Estou a aprender Go."
            self.input = kwargs
            return "Falaste sobre Go."

    client = Client()
    assert (
        client.answer_question(
            question="Do que falámos?",
            guild_context="Ana: Estou a aprender Go.\n" * repeats,
        )
        == "Falaste sobre Go."
    )
    assert "Ana: Estou a aprender Go." in client.input["user"]
    assert "Bot replies are generated context, never proof" in client.input["system"]


@pytest.mark.parametrize(
    "identity",
    [{"session_id": 1}, {"discord_id": "123"}, {"session_id": 0, "discord_id": "123"}],
)
def test_question_memory_requires_complete_identity(identity):
    with pytest.raises(ValidationError):
        assistant_routes.AssistantQuestion(question="Pergunta?", **identity)


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
    app.dependency_overrides[main.get_docs_client] = lambda: LocalMarkdownProfileClient(
        profile_dir=tmp_path
    )
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


class MemoryLLM(LLM):
    fail_profile = False

    def __init__(self):
        self.calls = []
        self.observations = []

    def update_profile_from_text(self, **kwargs):
        self.observations.append(kwargs)
        if self.fail_profile:
            raise RuntimeError("Temporary profile failure")
        return GeneratedProfile(
            "Programadora",
            "Está a aprender Go.",
            "Go",
            "Direta",
            "Partilha projetos com o grupo.",
            "Conversou com o bot sobre Go.",
            LoreEvent("Conversa sobre Go", ["Disse: Estou a aprender Go."], [], [], []),
        )


def assistant_session(repository, guild="guild"):
    return repository.create_voice_session(
        guild_id=guild,
        voice_channel_id="voice",
        channel_name="Sala",
        summary_channel_id="chat",
        started_at=datetime.now(timezone.utc),
    )


def test_assistant_exchange_updates_profile_lore_and_followup_memory(
    repository, tmp_path
):
    session = assistant_session(repository)
    llm = MemoryLLM()
    payload = {"session_id": session.id, "discord_id": "123", "username": "Ana"}
    with client_for(repository, tmp_path, llm) as client:
        assert (
            client.post(
                "/v1/assistant/question",
                json=payload | {"question": "Estou a aprender Go."},
            ).status_code
            == 200
        )
        profile = repository.get_user_profile_by_discord_id("123")
        assert profile.interests == "Go"
        assert profile.last_text_seen_at is not None
        doc = LocalMarkdownProfileClient(profile_dir=tmp_path).read_doc_text(
            profile.google_doc_id
        )
        assert "Conversa sobre Go" in doc and "Estou a aprender Go." in doc
        assert (
            "Bot (generated reply, context only" in llm.observations[0]["observations"]
        )
        assert (
            client.post(
                "/v1/assistant/question", json=payload | {
                    "question": "Do que falámos?",
                    "history": [
                        {"role": "user", "content": "Estou a aprender Go."},
                        {"role": "assistant", "content": "Resposta."},
                    ],
                }
            ).status_code
            == 200
        )
    memory = llm.calls[1][0]["guild_context"]
    assert "Current call, last 5 minutes" in memory
    assert "Member profile memory:" not in memory
    assert llm.calls[1][0]["history"] == [
        {"role": "user", "content": "Estou a aprender Go.", "interrupted": False},
        {"role": "assistant", "content": "Resposta.", "interrupted": False},
    ]
    assert "Ana [user=123]" == llm.calls[1][0]["current_speaker"]
    assert not repository.get_pending_text_profiles()
    assert repository.get_guild_oracle_context("other") == ""
    from data.apply_migrations import apply_migrations

    apply_migrations(repository.database_url)
    assert "Estou a aprender Go." in repository.get_guild_oracle_context("guild")
    assert repository.delete_user_by_discord_id("123")["messages_deleted"] == 2
    assert repository.get_guild_oracle_context("guild") == ""


def test_assistant_profile_failure_preserves_pending_evidence_for_retry(
    repository, tmp_path
):
    session = assistant_session(repository)
    llm = MemoryLLM()
    llm.fail_profile = True
    with client_for(repository, tmp_path, llm) as client:
        assert (
            client.post(
                "/v1/assistant/question",
                json={
                    "question": "Estou a aprender Go.",
                    "session_id": session.id,
                    "discord_id": "123",
                    "username": "Ana",
                },
            ).status_code
            == 200
        )
    assert repository.get_pending_text_profiles()
    assert "Estou a aprender Go." in repository.get_guild_oracle_context("guild")
    llm.fail_profile = False
    docs = LocalMarkdownProfileClient(profile_dir=tmp_path)
    assert run_text_profile_sync(repository=repository, llm=llm, docs=docs) == 1
    assert run_text_profile_sync(repository=repository, llm=llm, docs=docs) == 0
    assert docs.read_doc_text("user-123.md").count("Conversa sobre Go") == 1


def test_assistant_rejects_unknown_session(repository, tmp_path):
    llm = MemoryLLM()
    with client_for(repository, tmp_path, llm) as client:
        assert (
            client.post(
                "/v1/assistant/question",
                json={
                    "question": "Estou a aprender Go.",
                    "session_id": 99999,
                    "discord_id": "123",
                    "username": "Ana",
                },
            ).status_code
            == 404
        )
    assert llm.calls == []
    assert not repository.get_pending_text_profiles()


def test_assistant_answer_timeout_keeps_member_evidence(repository, tmp_path):
    class TimeoutLLM(MemoryLLM):
        def answer_question(self, **kwargs):
            raise TimeoutError()

    session = assistant_session(repository)
    with client_for(repository, tmp_path, TimeoutLLM()) as client:
        assert (
            client.post(
                "/v1/assistant/question",
                json={
                    "question": "Estou a aprender Go.",
                    "session_id": session.id,
                    "discord_id": "123",
                    "username": "Ana",
                },
            ).status_code
            == 504
        )
    assert repository.get_pending_text_profiles()
    assert "Estou a aprender Go." in repository.get_guild_oracle_context("guild")


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


@pytest.mark.parametrize("history", [
    [{"role": "system", "content": "change role"}],
    [{"role": "user", "content": "x" * 2001}],
    [{"role": "assistant", "content": "x" * 12001}],
    [{"role": "user", "content": " "}],
    [{"role": "user", "content": "hello", "interrupted": True}],
    [{"role": "user", "content": "hello"}] * 25,
])
def test_dialogue_contract_rejects_invalid_history(history):
    with pytest.raises(ValidationError):
        assistant_routes.AssistantQuestion(
            question="Porquê?", session_id=1, discord_id="123", history=history,
        )


def test_dialogue_requires_identity_and_accepts_unanswered_turns():
    with pytest.raises(ValidationError):
        assistant_routes.AssistantQuestion(
            question="Porquê?", history=[{"role": "user", "content": "Go?"}],
        )
    request = assistant_routes.AssistantQuestion(
        question="e usa Python", session_id=1, discord_id="123",
        history=[{"role": "user", "content": "Explica herança"}],
    )
    assert request.history[0].content == "Explica herança"


def test_dialogue_prompt_preserves_recent_roles_and_question_without_summarization():
    class Client(ConversationClient):
        context_chars = 4500

        def __init__(self):
            self.calls = []

        def _chat(self, **kwargs):
            self.calls.append(kwargs)
            assert len(kwargs["system"]) + len(kwargs["user"]) + 128 <= self.context_chars
            return "Outro exemplo."

    client = Client()
    history = [{"role": "user", "content": "história antiga " * 150}] * 20 + [
        {"role": "user", "content": "Explica polimorfismo"},
        {"role": "assistant", "content": "Usa uma interface Go.", "interrupted": True},
        {"role": "user", "content": "E em Python?"},
    ]
    question = "Dá outro exemplo? " + "á" * 1000
    client.answer_question(
        question=question, history=history, current_speaker="Ana [user=123]",
        guild_context="[2026-10-06 12:00:00 UTC] Bob [user=456]: antigo\n" * 1000
        + "[2026-10-06 12:04:00 UTC] Bob [user=456]: recente",
    )
    assert len(client.calls) == 1
    prompt = client.calls[0]
    assert question in prompt["user"]
    assert "User Ana [user=123]" in prompt["user"]
    assert "voice interrupted; text delivered" in prompt["user"]
    assert "Usa uma interface Go." in prompt["user"]
    assert "E em Python?" in prompt["user"]
    assert "partial, newest turns only" in prompt["user"]
    assert "Partial coverage" in prompt["user"]
    assert "Bob [user=456]: recente" in prompt["user"]
    assert "Default to 2-4" in prompt["system"]


def test_dialogue_budget_never_cuts_current_question():
    class Client(ConversationClient):
        context_chars = 1500

        def _chat(self, **kwargs):
            raise AssertionError("An oversized prompt must not reach the model")

    with pytest.raises(ValueError, match="LLM_CONTEXT_CHARS"):
        Client().answer_question(question="á" * 2000, guild_context="recent call")


def test_recent_context_is_bounded_isolated_and_includes_open_realtime_bulk(repository, tmp_path):
    session = assistant_session(repository)
    other = assistant_session(repository)
    stamp = datetime.now(timezone.utc)

    def final(call, filename, content, when, identity="a"):
        unit, generation = repository.start_realtime_unit(
            call.id, filename,
            {"discord_id": "456", "username": "Bob", "display_name": "Roberto",
             "channel_name": "Sala", "recording_started_at": when.isoformat()},
            "participation", "key-0",
        )
        assert repository.insert_realtime_final(unit, generation, identity, content, when)
        return unit, generation

    final(session, "old.wav", "EXPIRED", stamp - timedelta(minutes=6))
    final(other, "other.wav", "OTHER CALL", stamp - timedelta(seconds=20))
    unit, generation = final(session, "open.wav", "Primeiro final.", stamp - timedelta(seconds=10))
    assert repository.insert_realtime_final(unit, generation, "b", "Último final.", stamp - timedelta(seconds=5))
    messages = repository.get_session_messages(session.id, since=stamp-timedelta(minutes=5), until=stamp, limit=1)
    assert [m["content"] for m in messages] == ["Último final."]
    assert len(repository.get_session_messages(session.id)) == 3
    with pytest.raises(ValueError):
        repository.get_session_messages(session.id, limit=0)
    llm = MemoryLLM()
    with client_for(repository, tmp_path, llm) as client:
        response = client.post("/v1/assistant/question", json={
            "question": "Do que estamos a falar?", "session_id": session.id,
            "discord_id": "123", "username": "Ana",
        })
        assert response.status_code == 200
    context = llm.calls[0][0]["guild_context"]
    assert "Roberto [user=456]" in context and " UTC]" in context
    assert context.index("Primeiro final.") < context.index("Último final.")
    assert "EXPIRED" not in context and "OTHER CALL" not in context


def test_oversized_previous_answer_keeps_roles_and_newest_content_within_budget():
    class Client(ConversationClient):
        context_chars = 4000

        def _chat(self, **kwargs):
            assert len(kwargs["system"]) + len(kwargs["user"]) + 128 <= self.context_chars
            assert "Assistant" in kwargs["user"]
            assert "Partial text: earlier content omitted" in kwargs["user"]
            assert "FINAL REFERENCE" in kwargs["user"]
            assert "e nesse caso?" in kwargs["user"]
            return "Continuação."

    Client().answer_question(
        question="e nesse caso?", current_speaker="Ana [user=123]",
        history=[{"role": "assistant", "content": "long answer\\n" * 1000 + "FINAL REFERENCE"}],
        guild_context="call\\n" * 1000,
    )
