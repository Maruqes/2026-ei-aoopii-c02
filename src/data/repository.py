from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlparse

import psycopg2


CHUNK_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True)
class MessageInsert:
    content: str
    tstamp: datetime


@dataclass(frozen=True)
class ChunkInfo:
    id: int
    channel_name: str
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class TranscriptionInsertResult:
    user_id: int
    message_ids: list[int]
    affected_chunks: list[ChunkInfo]


@dataclass(frozen=True)
class TextMessageInsertResult:
    user_id: int
    message_id: int


@dataclass(frozen=True)
class VoiceSession:
    id: int
    guild_id: str
    voice_channel_id: str
    channel_name: str
    summary_channel_id: str | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    summary: str | None
    agent_error: str | None


@dataclass(frozen=True)
class SessionParticipant:
    user_id: int
    discord_id: str
    username: str
    display_name: str | None


@dataclass(frozen=True)
class PendingTextProfile:
    user_id: int
    discord_id: str
    username: str
    display_name: str | None
    last_text_seen_at: datetime | None
    latest_message_at: datetime


@dataclass(frozen=True)
class UserProfile:
    user_id: int
    discord_id: str
    username: str
    display_name: str | None
    summary: str
    interests: str
    communication_style: str
    known_facts: str
    recent_updates: str
    google_doc_id: str | None
    google_doc_url: str | None
    last_updated_at: datetime | None
    last_text_seen_at: datetime | None


def normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def chunk_window_for_timestamp(value: datetime) -> tuple[datetime, datetime]:
    value = normalize_timestamp(value)
    window_minute = 0 if value.minute < 30 else 30
    start = value.replace(minute=window_minute, second=0, microsecond=0)
    return start, start + CHUNK_WINDOW


def affected_windows(timestamps: Iterable[datetime]) -> list[tuple[datetime, datetime]]:
    windows = {chunk_window_for_timestamp(tstamp) for tstamp in timestamps}
    return sorted(windows, key=lambda window: window[0])


def format_chunk_rows(rows: Iterable[dict]) -> str:
    lines: list[str] = []
    for row in rows:
        tstamp = normalize_timestamp(row["tstamp"])
        username = row["username"]
        content = " ".join(str(row["content"]).split())
        if content:
            lines.append(f"[{tstamp:%H:%M}] {username}: {content}")
    return "\n".join(lines)


class DataRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def healthcheck(self) -> bool:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1
        finally:
            conn.close()

    def insert_transcription_segments(
        self,
        *,
        session_id: int | None = None,
        discord_id: str,
        username: str,
        display_name: str | None,
        channel_name: str,
        messages: list[MessageInsert],
    ) -> TranscriptionInsertResult:
        normalized_messages = [
            MessageInsert(content=message.content.strip(), tstamp=normalize_timestamp(message.tstamp))
            for message in messages
            if message.content.strip()
        ]

        conn = connect(self.database_url)
        try:
            user_id = self._upsert_user(conn, discord_id, username, display_name)
            message_ids = self._insert_messages(conn, user_id, session_id, channel_name, normalized_messages)
            chunks = self._rebuild_chunks(conn, channel_name, [message.tstamp for message in normalized_messages])
            conn.commit()
            return TranscriptionInsertResult(
                user_id=user_id,
                message_ids=message_ids,
                affected_chunks=chunks,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_text_message(
        self,
        *,
        guild_id: str,
        channel_id: str,
        channel_name: str,
        discord_message_id: str,
        discord_id: str,
        username: str,
        display_name: str | None,
        content: str,
        tstamp: datetime,
        edited_at: datetime | None = None,
    ) -> TextMessageInsertResult:
        content = " ".join(content.split())
        if not content:
            raise ValueError("content is required")

        conn = connect(self.database_url)
        try:
            user_id = self._upsert_user(conn, discord_id, username, display_name)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO messages (
                    user_id, session_id, source_type, guild_id, channel_id, channel_name,
                    discord_message_id, content, tstamp, edited_at
                )
                VALUES (%s, NULL, 'text', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (discord_message_id) WHERE discord_message_id IS NOT NULL DO UPDATE
                SET user_id = EXCLUDED.user_id,
                    guild_id = EXCLUDED.guild_id,
                    channel_id = EXCLUDED.channel_id,
                    channel_name = EXCLUDED.channel_name,
                    content = EXCLUDED.content,
                    tstamp = EXCLUDED.tstamp,
                    edited_at = COALESCE(EXCLUDED.edited_at, messages.edited_at)
                RETURNING id
                """,
                (
                    user_id,
                    guild_id.strip(),
                    channel_id.strip(),
                    channel_name.strip(),
                    discord_message_id.strip(),
                    content,
                    normalize_timestamp(tstamp),
                    normalize_timestamp(edited_at) if edited_at else None,
                ),
            )
            message_id = int(cur.fetchone()[0])
            conn.commit()
            return TextMessageInsertResult(user_id=user_id, message_id=message_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _upsert_user(
        self,
        conn,
        discord_id: str,
        username: str,
        display_name: str | None,
    ) -> int:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (discord_id, username, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (discord_id) DO UPDATE
            SET username = EXCLUDED.username,
                display_name = COALESCE(EXCLUDED.display_name, users.display_name)
            RETURNING id
            """,
            (discord_id, username, display_name),
        )
        return int(cur.fetchone()[0])

    def _insert_messages(
        self,
        conn,
        user_id: int,
        session_id: int | None,
        channel_name: str,
        messages: list[MessageInsert],
    ) -> list[int]:
        ids: list[int] = []
        cur = conn.cursor()
        for message in messages:
            cur.execute(
                """
                INSERT INTO messages (user_id, session_id, source_type, channel_name, content, tstamp)
                VALUES (%s, %s, 'voice', %s, %s, %s)
                RETURNING id
                """,
                (user_id, session_id, channel_name, message.content, message.tstamp),
            )
            ids.append(int(cur.fetchone()[0]))
        return ids

    def _rebuild_chunks(
        self,
        conn,
        channel_name: str,
        timestamps: list[datetime],
    ) -> list[ChunkInfo]:
        rebuilt: list[ChunkInfo] = []
        cur = conn.cursor()
        for start_at, end_at in affected_windows(timestamps):
            cur.execute(
                """
                SELECT m.tstamp, u.username, m.content
                FROM messages m
                JOIN users u ON u.id = m.user_id
                WHERE m.channel_name = %s
                  AND m.source_type = 'voice'
                  AND m.tstamp >= %s
                  AND m.tstamp < %s
                ORDER BY m.tstamp ASC, m.id ASC
                """,
                (channel_name, start_at, end_at),
            )
            rows = [
                {"tstamp": row[0], "username": row[1], "content": row[2]}
                for row in cur.fetchall()
            ]
            content = format_chunk_rows(rows)
            cur.execute(
                """
                INSERT INTO text_chunks (channel_name, content, start_at, end_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (channel_name, start_at, end_at) DO UPDATE
                SET content = EXCLUDED.content
                RETURNING id, channel_name, start_at, end_at
                """,
                (channel_name, content, start_at, end_at),
            )
            row = cur.fetchone()
            rebuilt.append(
                ChunkInfo(
                    id=int(row[0]),
                    channel_name=row[1],
                    start_at=row[2],
                    end_at=row[3],
                )
            )
        return rebuilt

    def create_voice_session(
        self,
        *,
        guild_id: str,
        voice_channel_id: str,
        channel_name: str,
        summary_channel_id: str | None,
        started_at: datetime,
    ) -> VoiceSession:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO voice_sessions (
                    guild_id, voice_channel_id, channel_name, summary_channel_id, started_at, status
                )
                VALUES (%s, %s, %s, %s, %s, 'open')
                RETURNING id, guild_id, voice_channel_id, channel_name, summary_channel_id,
                          started_at, ended_at, status, summary, agent_error
                """,
                (
                    guild_id.strip(),
                    voice_channel_id.strip(),
                    channel_name.strip() or "voice",
                    summary_channel_id.strip() if summary_channel_id else None,
                    normalize_timestamp(started_at),
                ),
            )
            session = voice_session_from_row(cur.fetchone())
            conn.commit()
            return session
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish_voice_session(self, session_id: int, ended_at: datetime) -> VoiceSession | None:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE voice_sessions
                SET ended_at = COALESCE(ended_at, %s),
                    status = CASE WHEN status = 'open' THEN 'finished' ELSE status END,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id, guild_id, voice_channel_id, channel_name, summary_channel_id,
                          started_at, ended_at, status, summary, agent_error
                """,
                (normalize_timestamp(ended_at), session_id),
            )
            row = cur.fetchone()
            conn.commit()
            return voice_session_from_row(row) if row else None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_voice_session(self, session_id: int) -> VoiceSession | None:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, guild_id, voice_channel_id, channel_name, summary_channel_id,
                       started_at, ended_at, status, summary, agent_error
                FROM voice_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            return voice_session_from_row(row) if row else None
        finally:
            conn.close()

    def start_recording(self, *, session_id: int | None, recording_filename: str, discord_id: str) -> int | None:
        if session_id is None:
            return None

        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO voice_recordings (session_id, recording_filename, discord_id, status, error)
                VALUES (%s, %s, %s, 'transcribing', NULL)
                ON CONFLICT (recording_filename) DO UPDATE
                SET session_id = EXCLUDED.session_id,
                    discord_id = EXCLUDED.discord_id,
                    status = 'transcribing',
                    error = NULL,
                    updated_at = NOW()
                RETURNING id
                """,
                (session_id, recording_filename, discord_id),
            )
            recording_id = int(cur.fetchone()[0])
            conn.commit()
            return recording_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_recording_completed(self, recording_id: int | None) -> None:
        self._mark_recording(recording_id, "completed", None)

    def mark_recording_failed(self, recording_id: int | None, error: str) -> None:
        self._mark_recording(recording_id, "failed", error)

    def _mark_recording(self, recording_id: int | None, status: str, error: str | None) -> None:
        if recording_id is None:
            return

        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE voice_recordings
                SET status = %s,
                    error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (status, error, recording_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_session_agent_run(self, session_id: int) -> bool:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE voice_sessions
                SET status = 'agent_running',
                    agent_error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'finished'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM voice_recordings
                      WHERE session_id = voice_sessions.id
                        AND status IN ('transcribing', 'pending')
                  )
                RETURNING id
                """,
                (session_id,),
            )
            claimed = cur.fetchone() is not None
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_session_agent_done(self, session_id: int, summary: str) -> None:
        self._mark_session_agent_result(session_id, "agent_done", summary, None)

    def mark_session_agent_failed(self, session_id: int, error: str) -> None:
        self._mark_session_agent_result(session_id, "agent_failed", None, error)

    def _mark_session_agent_result(
        self,
        session_id: int,
        status: str,
        summary: str | None,
        error: str | None,
    ) -> None:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE voice_sessions
                SET status = %s,
                    summary = COALESCE(%s, summary),
                    agent_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (status, summary, error, session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_session_messages(self, session_id: int) -> list[dict]:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT m.tstamp, u.discord_id, u.username, u.display_name, m.channel_name, m.content
                FROM messages m
                JOIN users u ON u.id = m.user_id
                WHERE m.session_id = %s
                ORDER BY m.tstamp ASC, m.id ASC
                """,
                (session_id,),
            )
            return [
                {
                    "tstamp": row[0],
                    "discord_id": row[1],
                    "username": row[2],
                    "display_name": row[3],
                    "channel_name": row[4],
                    "content": row[5],
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    def get_session_participants(self, session_id: int) -> list[SessionParticipant]:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT DISTINCT u.id, u.discord_id, u.username, u.display_name
                FROM messages m
                JOIN users u ON u.id = m.user_id
                WHERE m.session_id = %s
                ORDER BY u.username ASC
                """,
                (session_id,),
            )
            return [
                SessionParticipant(
                    user_id=int(row[0]),
                    discord_id=row[1],
                    username=row[2],
                    display_name=row[3],
                )
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    def get_user_profile_by_discord_id(self, discord_id: str) -> UserProfile | None:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.id, u.discord_id, u.username, u.display_name,
                       COALESCE(p.summary, ''),
                       COALESCE(p.interests, ''),
                       COALESCE(p.communication_style, ''),
                       COALESCE(p.known_facts, ''),
                       COALESCE(p.recent_updates, ''),
                       p.google_doc_id,
                       p.google_doc_url,
                       p.last_updated_at,
                       p.last_text_seen_at
                FROM users u
                LEFT JOIN user_profiles p ON p.user_id = u.id
                WHERE u.discord_id = %s
                """,
                (discord_id.strip(),),
            )
            row = cur.fetchone()
            return user_profile_from_row(row) if row else None
        finally:
            conn.close()

    def get_user_profile_by_user_id(self, user_id: int) -> UserProfile | None:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.id, u.discord_id, u.username, u.display_name,
                       COALESCE(p.summary, ''),
                       COALESCE(p.interests, ''),
                       COALESCE(p.communication_style, ''),
                       COALESCE(p.known_facts, ''),
                       COALESCE(p.recent_updates, ''),
                       p.google_doc_id,
                       p.google_doc_url,
                       p.last_updated_at,
                       p.last_text_seen_at
                FROM users u
                LEFT JOIN user_profiles p ON p.user_id = u.id
                WHERE u.id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return user_profile_from_row(row) if row else None
        finally:
            conn.close()

    def upsert_user_profile(
        self,
        *,
        user_id: int,
        summary: str,
        interests: str,
        communication_style: str,
        known_facts: str,
        recent_updates: str,
        google_doc_id: str | None,
        google_doc_url: str | None,
    ) -> None:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_profiles (
                    user_id, summary, interests, communication_style, known_facts,
                    recent_updates, google_doc_id, google_doc_url, last_updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET summary = EXCLUDED.summary,
                    interests = EXCLUDED.interests,
                    communication_style = EXCLUDED.communication_style,
                    known_facts = EXCLUDED.known_facts,
                    recent_updates = EXCLUDED.recent_updates,
                    google_doc_id = COALESCE(EXCLUDED.google_doc_id, user_profiles.google_doc_id),
                    google_doc_url = COALESCE(EXCLUDED.google_doc_url, user_profiles.google_doc_url),
                    last_updated_at = NOW()
                """,
                (
                    user_id,
                    summary.strip(),
                    interests.strip(),
                    communication_style.strip(),
                    known_facts.strip(),
                    recent_updates.strip(),
                    google_doc_id,
                    google_doc_url,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_pending_text_profiles(self) -> list[PendingTextProfile]:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.id, u.discord_id, u.username, u.display_name,
                       p.last_text_seen_at,
                       MAX(m.tstamp) AS latest_message_at
                FROM messages m
                JOIN users u ON u.id = m.user_id
                LEFT JOIN user_profiles p ON p.user_id = u.id
                WHERE m.source_type = 'text'
                  AND m.content <> ''
                  AND (
                      p.last_text_seen_at IS NULL
                      OR m.tstamp > p.last_text_seen_at
                  )
                GROUP BY u.id, u.discord_id, u.username, u.display_name, p.last_text_seen_at
                ORDER BY latest_message_at ASC
                """
            )
            return [
                PendingTextProfile(
                    user_id=int(row[0]),
                    discord_id=row[1],
                    username=row[2],
                    display_name=row[3],
                    last_text_seen_at=row[4],
                    latest_message_at=row[5],
                )
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    def get_text_messages_for_profile(self, user_id: int, after: datetime | None) -> list[dict]:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            if after is None:
                cur.execute(
                    """
                    SELECT m.tstamp, u.discord_id, u.username, u.display_name,
                           m.channel_name, m.content
                    FROM messages m
                    JOIN users u ON u.id = m.user_id
                    WHERE m.user_id = %s
                      AND m.source_type = 'text'
                      AND m.content <> ''
                    ORDER BY m.tstamp ASC, m.id ASC
                    """,
                    (user_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT m.tstamp, u.discord_id, u.username, u.display_name,
                           m.channel_name, m.content
                    FROM messages m
                    JOIN users u ON u.id = m.user_id
                    WHERE m.user_id = %s
                      AND m.source_type = 'text'
                      AND m.content <> ''
                      AND m.tstamp > %s
                    ORDER BY m.tstamp ASC, m.id ASC
                    """,
                    (user_id, normalize_timestamp(after)),
                )
            return [
                {
                    "tstamp": row[0],
                    "discord_id": row[1],
                    "username": row[2],
                    "display_name": row[3],
                    "channel_name": row[4],
                    "content": row[5],
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    def mark_user_text_profile_seen(self, user_id: int, seen_at: datetime) -> None:
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_profiles (user_id, last_text_seen_at)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET last_text_seen_at = GREATEST(
                        COALESCE(user_profiles.last_text_seen_at, '-infinity'::timestamptz),
                        EXCLUDED.last_text_seen_at
                    )
                """,
                (user_id, normalize_timestamp(seen_at)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def connect(database_url: str):
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("DATABASE_URL must use postgresql://")
    return psycopg2.connect(
        user=parsed.username,
        password=parsed.password,
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
    )


def voice_session_from_row(row) -> VoiceSession:
    return VoiceSession(
        id=int(row[0]),
        guild_id=row[1],
        voice_channel_id=row[2],
        channel_name=row[3],
        summary_channel_id=row[4],
        started_at=row[5],
        ended_at=row[6],
        status=row[7],
        summary=row[8],
        agent_error=row[9],
    )


def user_profile_from_row(row) -> UserProfile:
    return UserProfile(
        user_id=int(row[0]),
        discord_id=row[1],
        username=row[2],
        display_name=row[3],
        summary=row[4],
        interests=row[5],
        communication_style=row[6],
        known_facts=row[7],
        recent_updates=row[8],
        google_doc_id=row[9],
        google_doc_url=row[10],
        last_updated_at=row[11],
        last_text_seen_at=row[12],
    )
