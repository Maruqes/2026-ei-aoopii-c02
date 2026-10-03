from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from app import main
from app.config import Settings
from app.docs_client import LocalMarkdownProfileClient
from app.group_memory import run_group_memory_tick
from app.llm import ConversationClient, GeneratedProfile, LoreEvent
from fastapi.testclient import TestClient

from data import group_memory as memory
from data.repository import MessageInsert, connect


class MemoryLLM:
    def __init__(self):
        self.bulk_calls = []
        self.profile_calls = []
        self.fail_profile = False
        self.fail_bulk = False

    def analyze_group_bulk(self, **kwargs):
        self.bulk_calls.append(kwargs)
        if self.fail_bulk:
            raise RuntimeError("LLM unavailable")
        return {
            "summary": "Ana e Bob falaram sobre Go.",
            "lore": "O plano de Go correu mal.",
            "reaction_text": "Esse plano está no modo this is fine.",
            "speak": True,
            "gif_query": "this is fine dog",
        }

    def update_profile_from_text(self, **kwargs):
        self.profile_calls.append(kwargs)
        if self.fail_profile:
            raise RuntimeError("Profile unavailable")
        return GeneratedProfile(
            "Programador",
            "Programa em Go.",
            "Go",
            "Direto",
            "Ajuda o grupo.",
            "Discutiu Go.",
            LoreEvent("Plano de Go", ["Falou sobre Go."], [], [], []),
        )


def clock():
    now = datetime.now(timezone.utc)
    return datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, timezone.utc)


def add_message(
    repository,
    observed_at,
    *,
    guild="one",
    user="123",
    content="Estou a aprender Go.",
    spoken_at=None,
    voice=False,
):
    if voice:
        session = repository.create_voice_session(
            guild_id=guild,
            voice_channel_id="voice",
            channel_name="Sala",
            summary_channel_id="chat",
            started_at=observed_at,
        )
        inserted = repository.insert_transcription_segments(
            discord_id=user,
            username=user,
            display_name=None,
            channel_name="Sala",
            session_id=session.id,
            messages=[MessageInsert(content=content, tstamp=spoken_at or observed_at)],
        )
        message_id = inserted.message_ids[0]
    else:
        inserted = repository.insert_text_message(
            guild_id=guild,
            channel_id="chat",
            channel_name="Sala",
            discord_message_id=f"{guild}-{user}-{observed_at.isoformat()}-{content}",
            discord_id=user,
            username=user,
            display_name=None,
            content=content,
            tstamp=spoken_at or observed_at,
        )
        message_id = inserted.message_id
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE messages SET observed_at = %s WHERE id = %s",
                (observed_at, message_id),
            )
        conn.commit()
    return message_id


def tick(repository, tmp_path, llm, now, **changes):
    settings = Settings(database_url=repository.database_url, **changes)
    run_group_memory_tick(
        repository=repository,
        llm_factory=lambda: llm,
        docs=LocalMarkdownProfileClient(profile_dir=tmp_path),
        settings=settings,
        now=now,
    )


def test_closed_bulk_profiles_both_speakers_and_recalls_recent_lore(
    repository, tmp_path
):
    now = clock()
    llm = MemoryLLM()
    add_message(repository, now - timedelta(minutes=2), user="123")
    add_message(
        repository,
        now - timedelta(minutes=1),
        user="456",
        voice=True,
        content="O meu projeto em Go correu mal.",
    )
    add_message(
        repository, now + timedelta(seconds=1), content="Esta janela ainda está aberta."
    )
    tick(repository, tmp_path, llm, now)
    assert len(llm.bulk_calls) == 1 and len(llm.profile_calls) == 2
    assert "janela ainda" not in llm.bulk_calls[0]["observations"]
    assert all("Only this member" in c["observations"] for c in llm.profile_calls)
    assert repository.get_user_profile_by_discord_id("123").interests == "Go"
    assert repository.get_user_profile_by_discord_id("456").interests == "Go"
    assert "Plano de Go" in (tmp_path / "user-456.md").read_text()
    assert "O plano de Go correu mal." in repository.get_guild_oracle_context(
        "one", "plano"
    )
    assert repository.get_guild_oracle_context("two") == ""
    assert len(memory.pending_reactions(repository, now, 5)) == 1
    tick(repository, tmp_path, llm, now)
    assert len(llm.bulk_calls) == 1 and len(llm.profile_calls) == 2
    tick(repository, tmp_path, llm, now + timedelta(minutes=5))
    assert len(llm.bulk_calls) == 2
    assert "Go correu mal" in llm.bulk_calls[1]["previous_bulks"]
    assert llm.bulk_calls[1]["reaction_allowed"] is False
    assert len(memory.recent_bulks(repository, "one")) == 2


def test_late_transcript_is_grouped_by_receipt_time_and_keeps_original_date(
    repository, tmp_path
):
    now = clock()
    old = now - timedelta(hours=2)
    llm = MemoryLLM()
    add_message(repository, now - timedelta(minutes=1), spoken_at=old, voice=True)
    tick(repository, tmp_path, llm, now)
    assert old.strftime("%Y-%m-%d %H:%M:%S") in llm.bulk_calls[0]["observations"]
    bulk = memory.recent_bulks(repository, "one")[0]
    assert bulk["start_at"] == now - timedelta(minutes=5)


def test_bulk_and_profile_failures_are_recoverable_and_lore_is_not_duplicated(
    repository, tmp_path
):
    now = clock()
    llm = MemoryLLM()
    add_message(repository, now - timedelta(minutes=1))
    llm.fail_bulk = True
    tick(repository, tmp_path, llm, now)
    assert memory.recent_bulks(repository, "one") == []
    llm.fail_bulk = False
    llm.fail_profile = True
    tick(repository, tmp_path, llm, now)
    assert len(memory.profile_jobs(repository, "one")) == 1
    llm.fail_profile = False
    tick(repository, tmp_path, llm, now)
    assert memory.profile_jobs(repository, "one") == []
    tick(repository, tmp_path, llm, now)
    assert (tmp_path / "user-123.md").read_text().count("Plano de Go") == 1


def test_reaction_claim_expires_and_disabling_assistant_keeps_memory_updates(
    repository, tmp_path
):
    now = clock()
    llm = MemoryLLM()
    add_message(repository, now - timedelta(minutes=1))
    tick(repository, tmp_path, llm, now)
    reaction = memory.pending_reactions(repository, now, 5)[0]
    assert not memory.claim_reaction(
        repository, reaction["id"], now + timedelta(minutes=6), 5
    )
    repository.update_assistant_settings("one", {"enabled": False})
    assert memory.pending_reactions(repository, now, 5) == []
    assert not memory.claim_reaction(repository, reaction["id"], now, 5)
    repository.update_assistant_settings("one", {"enabled": True})
    assert memory.claim_reaction(repository, reaction["id"], now, 5)
    assert not memory.claim_reaction(repository, reaction["id"], now, 5)
    memory.finish_reaction(repository, reaction["id"], "sent")
    assert memory.pending_reactions(repository, now, 5) == []
    add_message(repository, now + timedelta(minutes=1))
    tick(
        repository,
        tmp_path,
        llm,
        now + timedelta(minutes=5),
        group_memory_reactions_enabled=False,
    )
    assert memory.recent_bulks(repository, "one")[0]["reaction_text"] == ""
    repository.delete_user_by_discord_id("123")
    assert memory.recent_bulks(repository, "one") == []
    assert repository.get_guild_oracle_context("one") == ""


def test_memory_routes_and_bounds(repository):
    service = main.create_app()
    service.dependency_overrides[main.get_repository] = lambda: repository
    service.dependency_overrides[main.get_settings] = lambda: Settings(
        database_url=repository.database_url
    )
    with closing(TestClient(service)) as client:
        assert client.get("/v1/guilds/one/memory").json() == {
            "bulk_minutes": 5,
            "context_bulks": 3,
            "bulks": [],
        }
        assert client.get("/v1/memory/reactions").json() == []
        assert client.post("/v1/memory/reactions/999/claim", json={}).status_code == 409
        assert (
            client.post(
                "/v1/memory/reactions/999/result", json={"status": "pending"}
            ).status_code
            == 422
        )


def test_late_commit_extends_bulk_and_forget_survives_source_replacement(
    repository, tmp_path
):
    now = clock()
    llm = MemoryLLM()
    add_message(repository, now - timedelta(minutes=2), voice=True)
    tick(repository, tmp_path, llm, now)
    bulk_id = memory.recent_bulks(repository, "one")[0]["id"]
    add_message(
        repository,
        now - timedelta(minutes=1),
        voice=True,
        content="Afinal o projeto em Go já funciona.",
    )
    tick(repository, tmp_path, llm, now)
    assert memory.recent_bulks(repository, "one")[0]["id"] == bulk_id
    assert len(memory.recent_bulks(repository, "one")) == 1
    assert len(memory.pending_reactions(repository, now, 5)) == 1
    assert len(llm.profile_calls) == 2
    # Batch replacement can remove every source row of an earlier Realtime bulk.
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messages")
        conn.commit()
    assert memory.recent_bulks(repository, "one")
    repository.delete_user_by_discord_id("123")
    assert memory.recent_bulks(repository, "one") == []


def test_bulk_prompt_memes_and_provider_output_validation():
    class Client(ConversationClient):
        output = {
            "summary": "Go",
            "lore": "Plano falhou",
            "reaction_text": "This is fine.",
            "speak": True,
            "gif_query": "this is fine dog",
        }

        def _chat(self, **kwargs):
            self.input = kwargs
            assert (
                len(kwargs["system"]) + len(kwargs["user"]) + 128 <= self.context_chars
            )
            return json.dumps(self.output)

    client = Client()
    result = client.analyze_group_bulk(
        observations="Ana: O projeto correu mal.",
        previous_bulks="",
        reaction_allowed=False,
    )
    assert result["reaction_text"] == result["gif_query"] == "" and not result["speak"]
    assert "MEME search" in client.input["system"]
    client.output = client.output | {"speak": "yes"}
    with pytest.raises(ValueError):
        client.analyze_group_bulk(
            observations="Go", previous_bulks="", reaction_allowed=True
        )


def test_memory_config_bounds(monkeypatch):
    monkeypatch.setenv("GROUP_MEMORY_BULK_MINUTES", "0")
    monkeypatch.setenv("GROUP_MEMORY_CONTEXT_BULKS", "9")
    assert Settings.from_env().group_memory_bulk_minutes == 1
    assert Settings.from_env().group_memory_context_bulks == 3
