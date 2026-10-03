from __future__ import annotations

import json
import random
from contextlib import closing, contextmanager
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
    response_language: str = "pt"


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
    anthropologist_title: str
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
        line = format_context_message_row(row, time_only=True)
        if line:
            lines.append(line)
    return "\n".join(lines)


def format_context_message_row(row: dict, *, time_only: bool = False) -> str:
    tstamp = normalize_timestamp(row["tstamp"])
    username = (
        row.get("display_name")
        or row.get("username")
        or row.get("discord_id")
        or "unknown"
    )
    content = " ".join(str(row.get("content", "")).split())
    if not content:
        return ""
    if time_only:
        return f"[{tstamp:%H:%M}] {username}: {content}"
    channel_name = str(row.get("channel_name") or "").strip()
    source_type = str(row.get("source_type") or "").strip()
    prefix = f"[{tstamp:%Y-%m-%d %H:%M}]"
    if channel_name:
        prefix += f" ({channel_name})"
    if source_type:
        prefix += f" [{source_type}]"
    return f"{prefix} {username}: {content}"


def display_name_from_row(
    username: str, display_name: str | None, discord_id: str
) -> str:
    for value in (display_name, username, discord_id):
        cleaned = str(value or "").strip()
        if cleaned:
            return cleaned
    return "unknown"


class DataRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def healthcheck(self) -> bool:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1

    def get_health_details(self) -> dict:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT status, COUNT(*)::int
                FROM voice_recordings
                GROUP BY status
                """
            )
            counts = {row[0]: int(row[1]) for row in cur.fetchall()}
            cur.execute(
                """
                SELECT status, recording_filename, updated_at
                FROM voice_recordings
                ORDER BY updated_at DESC
                LIMIT 1
                """
            )
            last_row = cur.fetchone()
            cur.execute(
                "SELECT status, COUNT(*)::int FROM voice_sessions GROUP BY status"
            )
            sessions = dict(cur.fetchall())
            cur.execute(
                "SELECT status, COUNT(*)::int FROM voice_profile_jobs GROUP BY status"
            )
            profiles = dict(cur.fetchall())
            return {
                "sessions_pending": sessions.get("finished", 0)
                + sessions.get("agent_running", 0),
                "sessions_failed": sessions.get("agent_failed", 0),
                "voice_profiles_pending": profiles.get("pending", 0),
                "voice_profiles_failed": profiles.get("failed", 0),
                "recordings_transcribing": counts.get("transcribing", 0)
                + counts.get("pending", 0),
                "recordings_failed": counts.get("failed", 0),
                "recordings_completed": counts.get("completed", 0),
                "last_recording_status": last_row[0] if last_row else None,
                "last_recording_filename": last_row[1] if last_row else None,
                "last_recording_at": last_row[2] if last_row else None,
            }

    def delete_user_by_discord_id(
        self, discord_id: str, *, remove_recording=None
    ) -> dict | None:
        with closing(connect(self.database_url)) as conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM users WHERE discord_id = %s", (discord_id.strip(),)
                )
                row = cur.fetchone()
                user_id = int(row[0]) if row else None
                # Never erase input owned by an active worker. Explicit erasure can be retried.
                cur.execute(
                    "SELECT id FROM voice_recordings WHERE discord_id = %s ORDER BY id",
                    (discord_id.strip(),),
                )
                recording_ids = [item[0] for item in cur.fetchall()]
                if user_id is None and not recording_ids:
                    return None
                for recording_id in recording_ids:
                    cur.execute(
                        "SELECT pg_try_advisory_xact_lock(%s, %s)", (101, recording_id)
                    )
                    if not cur.fetchone()[0]:
                        raise ValueError(
                            "Transcription in progress; try /forget after it finishes"
                        )
                cur.execute(
                    "SELECT DISTINCT vr.recording_filename FROM voice_recordings vr "
                    "WHERE vr.discord_id = %s AND NOT EXISTS (SELECT 1 FROM voice_recordings other "
                    "WHERE other.recording_filename = vr.recording_filename AND other.discord_id <> %s)",
                    (discord_id.strip(), discord_id.strip()),
                )
                recording_filenames = [item[0] for item in cur.fetchall()]
                if remove_recording is not None:
                    # Keep DB ownership on unlink failure so explicit erasure can be retried.
                    for filename in recording_filenames:
                        remove_recording(filename)
                cur.execute(
                    "SELECT google_doc_id FROM user_profiles WHERE user_id = %s",
                    (user_id,),
                )
                profile_row = cur.fetchone()
                google_doc_id = profile_row[0] if profile_row else None

                cur.execute(
                    """
                    SELECT DISTINCT channel_name
                    FROM messages
                    WHERE user_id = %s
                      AND source_type = 'voice'
                      AND channel_name IS NOT NULL
                    """,
                    (user_id,),
                )
                channel_names = [channel_row[0] for channel_row in cur.fetchall()]

                cur.execute(
                    "SELECT COUNT(*)::int FROM messages WHERE user_id = %s", (user_id,)
                )
                messages_deleted = int(cur.fetchone()[0])

                cur.execute("DELETE FROM messages WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM user_profiles WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                cur.execute(
                    "DELETE FROM voice_recordings WHERE discord_id = %s",
                    (discord_id.strip(),),
                )

                for channel_name in channel_names:
                    self._rebuild_all_voice_chunks_for_channel(conn, channel_name)

                conn.commit()
                return {
                    "user_id": user_id,
                    "messages_deleted": messages_deleted,
                    "google_doc_id": google_doc_id,
                }
            except Exception:
                conn.rollback()
                raise

    def insert_transcription_segments(
        self,
        *,
        session_id: int | None = None,
        recording_id: int | None = None,
        duration_seconds: float | None = None,
        provider_completed_at: datetime | None = None,
        discord_id: str,
        username: str,
        display_name: str | None,
        channel_name: str,
        messages: list[MessageInsert],
    ) -> TranscriptionInsertResult:
        normalized_messages = [
            MessageInsert(
                content=message.content.strip(),
                tstamp=normalize_timestamp(message.tstamp),
            )
            for message in messages
            if message.content.strip()
        ]

        with closing(connect(self.database_url)) as conn:
            try:
                if recording_id is not None:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT status FROM voice_recordings WHERE id = %s FOR UPDATE",
                        (recording_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise ValueError(
                            "Recording was removed while transcription was running"
                        )
                    if row[0] in {"completed", "discarded_no_credits", "discarded_privacy"}:
                        return TranscriptionInsertResult(0, [], [])
                    if row[0] not in {"pending", "transcribing"}:
                        raise ValueError("Recording is still owned by Realtime")
                    # Batch replaces precisely this WAV's provisional Realtime text.
                    cur.execute("DELETE FROM messages WHERE recording_id = %s", (recording_id,))
                    self._rebuild_all_voice_chunks_for_channel(conn, channel_name)
                user_id = self._upsert_user(conn, discord_id, username, display_name)
                message_ids = self._insert_messages(
                    conn, user_id, session_id, channel_name, normalized_messages
                )
                if recording_id is not None and message_ids:
                    cur.execute("UPDATE messages SET recording_id = %s WHERE id = ANY(%s)", (recording_id, message_ids))
                chunks = self._rebuild_chunks(
                    conn,
                    channel_name,
                    [message.tstamp for message in normalized_messages],
                )
                if recording_id is not None:
                    cur.execute(
                        "UPDATE voice_recordings SET status = 'completed', error = NULL, "
                        "duration_seconds = %s, provider_completed_at = %s, completed_at = NOW(), updated_at = NOW() WHERE id = %s",
                        (duration_seconds, provider_completed_at, recording_id),
                    )
                conn.commit()
                return TranscriptionInsertResult(
                    user_id=user_id,
                    message_ids=message_ids,
                    affected_chunks=chunks,
                )
            except Exception:
                conn.rollback()
                raise

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

        with closing(connect(self.database_url)) as conn:
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
                    WHERE COALESCE(EXCLUDED.edited_at, EXCLUDED.tstamp) >= COALESCE(messages.edited_at, messages.tstamp)
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
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "SELECT id FROM messages WHERE discord_message_id = %s",
                        (discord_message_id.strip(),),
                    )
                    row = cur.fetchone()
                message_id = int(row[0])
                conn.commit()
                return TextMessageInsertResult(user_id=user_id, message_id=message_id)
            except Exception:
                conn.rollback()
                raise

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

    def _rebuild_all_voice_chunks_for_channel(self, conn, channel_name: str) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT start_at, end_at
            FROM text_chunks
            WHERE channel_name = %s
            ORDER BY start_at ASC
            """,
            (channel_name,),
        )
        windows = cur.fetchall()
        for start_at, end_at in windows:
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
                UPDATE text_chunks
                SET content = %s
                WHERE channel_name = %s
                  AND start_at = %s
                  AND end_at = %s
                """,
                (content, channel_name, start_at, end_at),
            )

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
        with closing(connect(self.database_url)) as conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO voice_sessions (
                        guild_id, voice_channel_id, channel_name, summary_channel_id, started_at, status
                    )
                    VALUES (%s, %s, %s, %s, %s, 'open')
                    RETURNING id, guild_id, voice_channel_id, channel_name, summary_channel_id,
                              started_at, ended_at, status, summary, agent_error, response_language
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

    def finish_voice_session(
        self, session_id: int, ended_at: datetime, language: str = "pt"
    ) -> VoiceSession | None:
        with closing(connect(self.database_url)) as conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE voice_sessions
                    SET ended_at = COALESCE(ended_at, %s),
                        status = CASE WHEN status = 'open' THEN 'finished' ELSE status END,
                        response_language = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, guild_id, voice_channel_id, channel_name, summary_channel_id,
                              started_at, ended_at, status, summary, agent_error, response_language
                    """,
                    (normalize_timestamp(ended_at), language, session_id),
                )
                row = cur.fetchone()
                conn.commit()
                return voice_session_from_row(row) if row else None
            except Exception:
                conn.rollback()
                raise

    def get_voice_session(self, session_id: int) -> VoiceSession | None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, guild_id, voice_channel_id, channel_name, summary_channel_id,
                       started_at, ended_at, status, summary, agent_error, response_language
                FROM voice_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            return voice_session_from_row(row) if row else None

    def list_voice_sessions(
        self,
        guild_id: str,
        *,
        limit: int = 10,
        voice_channel_id: str | None = None,
    ) -> list[VoiceSession]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            params: list = [guild_id.strip()]
            channel_filter = ""
            if voice_channel_id and voice_channel_id.strip():
                channel_filter = "AND voice_channel_id = %s"
                params.append(voice_channel_id.strip())
            params.append(max(1, min(limit, 50)))
            cur.execute(
                f"""
                SELECT id, guild_id, voice_channel_id, channel_name, summary_channel_id,
                       started_at, ended_at, status, summary, agent_error, response_language
                FROM voice_sessions
                WHERE guild_id = %s
                {channel_filter}
                ORDER BY started_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return [voice_session_from_row(row) for row in cur.fetchall()]

    def get_latest_voice_session(
        self,
        guild_id: str,
        *,
        voice_channel_id: str | None = None,
    ) -> VoiceSession | None:
        sessions = self.list_voice_sessions(
            guild_id,
            limit=1,
            voice_channel_id=voice_channel_id,
        )
        return sessions[0] if sessions else None

    def get_random_guess_message(self, guild_id: str) -> dict | None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT m.id, m.content, m.session_id, vs.channel_name,
                       u.discord_id, u.username, u.display_name
                FROM messages m
                JOIN users u ON u.id = m.user_id
                JOIN voice_sessions vs ON vs.id = m.session_id
                WHERE vs.guild_id = %s
                  AND m.source_type = 'voice'
                  AND m.session_id IS NOT NULL
                  AND length(trim(m.content)) >= 20
                  AND (
                      SELECT COUNT(*)::int
                      FROM regexp_split_to_table(trim(m.content), '\\s+') AS word
                      WHERE word <> ''
                  ) >= 4
                ORDER BY RANDOM()
                LIMIT 1
                """,
                (guild_id.strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None

            message_id = int(row[0])
            content = str(row[1]).strip()
            session_id = int(row[2])
            channel_name = row[3]
            correct_discord_id = row[4]
            correct_name = display_name_from_row(row[5], row[6], row[4])

            cur.execute(
                """
                SELECT DISTINCT u.discord_id, u.username, u.display_name
                FROM messages m
                JOIN users u ON u.id = m.user_id
                WHERE m.session_id = %s
                ORDER BY u.username ASC
                """,
                (session_id,),
            )
            participants = [
                {
                    "discord_id": participant_row[0],
                    "display_name": display_name_from_row(
                        participant_row[1],
                        participant_row[2],
                        participant_row[0],
                    ),
                }
                for participant_row in cur.fetchall()
            ]

            option_names = [participant["display_name"] for participant in participants]
            if correct_name not in option_names:
                option_names.append(correct_name)
            option_names = list(dict.fromkeys(option_names))
            random.shuffle(option_names)

            return {
                "message_id": message_id,
                "quote": content,
                "session_id": session_id,
                "channel_name": channel_name,
                "correct_discord_id": correct_discord_id,
                "correct_display_name": correct_name,
                "options": option_names,
            }

    def get_text_digest(
        self, guild_id: str, *, hours: int = 24, channel_id: str | None = None
    ) -> tuple[str, int, bool]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT m.tstamp, u.username, u.display_name, m.channel_name, m.content "
                "FROM messages m JOIN users u ON u.id = m.user_id "
                "WHERE m.guild_id = %s AND m.source_type = 'text' "
                "AND m.tstamp >= NOW() - %s * INTERVAL '1 hour' "
                "AND (%s::text IS NULL OR m.channel_id = %s) "
                "ORDER BY m.tstamp DESC, m.id DESC LIMIT 2001",
                (guild_id.strip(), hours, channel_id, channel_id),
            )
            rows = cur.fetchall()
            limited = len(rows) > 2000
            rows = rows[:2000]
            lines = [
                format_context_message_row(
                    dict(
                        zip(
                            (
                                "tstamp",
                                "username",
                                "display_name",
                                "channel_name",
                                "content",
                            ),
                            row,
                        )
                    )
                )
                for row in reversed(rows)
            ]
            return "\n".join(lines), len(rows), limited

    def get_guild_oracle_context(self, guild_id: str, question: str = "") -> str:
        with closing(connect(self.database_url)) as conn:
            sections: list[str] = []
            cur = conn.cursor()
            cur.execute(
                """
                SELECT channel_name, started_at, ended_at, summary, status
                FROM voice_sessions
                WHERE guild_id = %s
                  AND summary IS NOT NULL
                  AND trim(summary) <> ''
                ORDER BY started_at DESC
                LIMIT 5
                """,
                (guild_id.strip(),),
            )
            summary_lines: list[str] = []
            for row in cur.fetchall():
                started = normalize_timestamp(row[1])
                channel_name = row[0] or "voice"
                summary_lines.append(
                    f"Voice session {started:%Y-%m-%d %H:%M} in {channel_name}:\n{row[3].strip()}"
                )
            if summary_lines:
                sections.append(
                    "Recent voice session summaries:\n" + "\n\n".join(summary_lines)
                )

            cur.execute(
                """
                SELECT m.tstamp, u.discord_id, u.username, u.display_name,
                       m.channel_name, m.content, m.source_type
                FROM messages m
                JOIN users u ON u.id = m.user_id
                LEFT JOIN voice_sessions vs ON vs.id = m.session_id
                WHERE m.guild_id = %s OR vs.guild_id = %s
                ORDER BY m.tstamp DESC
                LIMIT 250
                """,
                (guild_id.strip(), guild_id.strip()),
            )
            message_rows = [
                {
                    "tstamp": row[0],
                    "discord_id": row[1],
                    "username": row[2],
                    "display_name": row[3],
                    "channel_name": row[4],
                    "content": row[5],
                    "source_type": row[6],
                }
                for row in cur.fetchall()
            ]
            message_rows.reverse()
            message_lines = [format_context_message_row(row) for row in message_rows]
            message_lines = [line for line in message_lines if line]
            if message_lines:
                sections.append(
                    "Recent messages (text and voice):\n" + "\n".join(message_lines)
                )

            stopwords = {
                "about",
                "what",
                "when",
                "where",
                "this",
                "that",
                "with",
                "have",
                "does",
                "quem",
                "qual",
                "quando",
                "onde",
                "sobre",
                "para",
                "como",
                "esta",
                "este",
                "isso",
                "isto",
                "mais",
                "menos",
                "fazer",
            }
            terms = list(
                dict.fromkeys(
                    word.strip('.,!?;:"()').lower()
                    for word in question.split()
                    if len(word) >= 4 and word.lower() not in stopwords
                )
            )[:8]
            if terms:
                predicates = " OR ".join("m.content ILIKE %s" for _ in terms)
                patterns = [
                    "%"
                    + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + "%"
                    for term in terms
                ]
                cur.execute(
                    "SELECT m.tstamp, u.username, m.channel_name, m.content FROM messages m "
                    "JOIN users u ON u.id = m.user_id LEFT JOIN voice_sessions vs ON vs.id = m.session_id "
                    "WHERE (m.guild_id = %s OR vs.guild_id = %s) AND ("
                    + predicates
                    + ") "
                    "ORDER BY m.tstamp DESC, m.id DESC LIMIT 100",
                    (guild_id.strip(), guild_id.strip(), *patterns),
                )
                relevant = [
                    format_context_message_row(
                        {
                            "tstamp": r[0],
                            "username": r[1],
                            "channel_name": r[2],
                            "content": r[3],
                        }
                    )
                    for r in cur.fetchall()
                ]
                if relevant:
                    sections.insert(
                        0,
                        "Messages matching the question (newest first):\n"
                        + "\n".join(relevant),
                    )
            return "\n\n".join(sections).strip()

    def get_recordings_for_cleanup(self, filenames: list[str]) -> dict[str, list[dict]]:
        if not filenames:
            return {}
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, recording_filename, discord_id, session_id, status FROM voice_recordings WHERE recording_filename = ANY(%s)",
                (filenames,),
            )
            result: dict[str, list[dict]] = {}
            for row in cur.fetchall():
                result.setdefault(row[1], []).append(
                    dict(
                        zip(
                            (
                                "id",
                                "recording_filename",
                                "discord_id",
                                "session_id",
                                "status",
                            ),
                            row,
                        )
                    )
                )
            return result

    def recording_receipt(
        self, filename: str, discord_id: str, session_id: int | None
    ) -> dict | None:
        rows = self.get_recordings_for_cleanup([filename]).get(filename, [])
        if not rows:
            return None
        if any(
            row["discord_id"] != discord_id or row["session_id"] != session_id
            for row in rows
        ):
            raise ValueError("Recording filename belongs to different metadata")
        return rows[0]

    def start_recording(
        self,
        *,
        session_id: int | None,
        recording_filename: str,
        discord_id: str,
        metadata: dict | None = None,
    ) -> int:
        with closing(connect(self.database_url)) as conn:
            try:
                cur = conn.cursor()
                if session_id is not None:
                    cur.execute(
                        "SELECT status, credits_exhausted FROM voice_sessions WHERE id = %s FOR UPDATE",
                        (session_id,),
                    )
                    session = cur.fetchone()
                    if not session or session[0] != "open":
                        # Duplicate submissions after finish remain idempotent.
                        cur.execute(
                            "SELECT id FROM voice_recordings WHERE recording_filename = %s AND session_id = %s AND discord_id = %s",
                            (recording_filename, session_id, discord_id),
                        )
                        previous = cur.fetchone()
                        if not previous:
                            raise ValueError("Session is closed or does not exist")
                cur.execute(
                    "INSERT INTO voice_recordings (session_id, recording_filename, discord_id, status, metadata) "
                    "VALUES (%s, %s, %s, 'pending', %s::jsonb) "
                    "ON CONFLICT (recording_filename) DO NOTHING RETURNING id",
                    (
                        session_id,
                        recording_filename,
                        discord_id,
                        json.dumps(metadata) if metadata else None,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "SELECT id FROM voice_recordings WHERE recording_filename = %s AND discord_id = %s AND session_id IS NOT DISTINCT FROM %s",
                        (recording_filename, discord_id, session_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise ValueError(
                            "Recording filename belongs to different metadata"
                        )
                recording_id = int(row[0])
                if session_id is not None and session[1]:
                    cur.execute("UPDATE voice_recordings SET status = 'discarded_no_credits', generation = generation + 1, cleanup_pending = TRUE WHERE id = %s AND status <> 'completed'", (recording_id,))
                else:
                    # The bot admits recovery only after closing the WAV and its WS.
                    cur.execute("UPDATE voice_recordings SET status = 'pending', generation = generation + 1 WHERE id = %s AND status IN ('streaming', 'fallback_pending')", (recording_id,))
                conn.commit()
                return recording_id
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def job_lock(self, namespace: int, job_id: int, *, wait: bool = False):
        """Session advisory locks survive commits, release automatically on process death."""
        conn = connect(self.database_url)
        try:
            cur = conn.cursor()
            if wait:
                cur.execute("SELECT pg_advisory_lock(%s, %s)", (namespace, job_id))
                locked = True
            else:
                cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (namespace, job_id))
                locked = bool(cur.fetchone()[0])
            yield locked
        finally:
            if not conn.closed:
                conn.rollback()
                # Closing releases the session advisory lock even after an exception.
                conn.close()

    def retry_session(self, session_id: int) -> None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM voice_sessions WHERE id = %s FOR UPDATE", (session_id,)
            )
            cur.execute(
                "UPDATE voice_profile_jobs pj SET status = 'pending', error = NULL, revision = revision + 1 "
                "WHERE pj.session_id = %s AND (pj.status = 'failed' OR EXISTS ("
                "SELECT 1 FROM voice_recordings vr JOIN users u ON u.discord_id = vr.discord_id "
                "WHERE vr.session_id = pj.session_id AND vr.status = 'failed' AND u.id = pj.user_id))",
                (session_id,),
            )
            cur.execute(
                "UPDATE voice_recordings SET status = 'pending', error = NULL, updated_at = NOW() "
                "WHERE session_id = %s AND status = 'failed' AND metadata IS NOT NULL",
                (session_id,),
            )
            cur.execute(
                "UPDATE voice_sessions SET status = 'finished', summary = NULL, agent_error = NULL, updated_at = NOW() WHERE id = %s",
                (session_id,),
            )
            conn.commit()

    def get_recording_jobs(self) -> list[dict]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, session_id, recording_filename, metadata, provider_job_id, provider_key_name "
                "FROM voice_recordings WHERE status IN ('pending', 'transcribing') AND metadata IS NOT NULL ORDER BY id LIMIT 100"
            )
            return [
                dict(
                    zip(
                        (
                            "id",
                            "session_id",
                            "recording_filename",
                            "metadata",
                            "provider_job_id",
                            "provider_key_name",
                        ),
                        row,
                    )
                )
                for row in cur.fetchall()
            ]

    def begin_recording_job(self, recording_id: int) -> bool:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE voice_recordings SET status = 'transcribing', updated_at = NOW() "
                "WHERE id = %s AND status IN ('pending', 'transcribing') RETURNING id",
                (recording_id,),
            )
            claimed = cur.fetchone() is not None
            conn.commit()
            return claimed

    def save_provider_job(self, recording_id: int, job_id: str, key_name: str) -> None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE voice_recordings SET provider_job_id = %s, provider_key_name = %s, updated_at = NOW() WHERE id = %s AND status IN ('pending', 'transcribing') RETURNING id",
                (job_id, key_name, recording_id),
            )
            if cur.fetchone() is None:
                raise ValueError("Recording cancelled")
            conn.commit()

    def get_unfinished_session_ids(self) -> list[int]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT vs.id FROM voice_sessions vs WHERE vs.status IN ('finished', 'agent_running') "
                "AND NOT EXISTS (SELECT 1 FROM voice_recordings vr WHERE vr.session_id = vs.id "
                "AND vr.status IN ('pending', 'transcribing', 'streaming', 'fallback_pending')) ORDER BY vs.id LIMIT 100"
            )
            return [int(row[0]) for row in cur.fetchall()]

    def get_session_recording_counts(self, session_id: int) -> dict[str, int]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT status, COUNT(*) FROM voice_recordings WHERE session_id = %s GROUP BY status",
                (session_id,),
            )
            return {row[0]: int(row[1]) for row in cur.fetchall()}

    def get_local_speechmatics_hours(self) -> dict[str, float]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT provider_key_name, SUM(duration_seconds) / 3600.0 FROM voice_recordings "
                "WHERE status = 'completed' AND provider_key_name IS NOT NULL "
                "AND provider_completed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
                "AND provider_completed_at < (date_trunc('day', NOW() AT TIME ZONE 'UTC') + INTERVAL '1 day') AT TIME ZONE 'UTC' "
                "GROUP BY provider_key_name"
            )
            return {row[0]: float(row[1] or 0) for row in cur.fetchall()}

    def mark_recording_completed(self, recording_id: int | None) -> None:
        self._mark_recording(recording_id, "completed", None)

    def mark_recording_failed(self, recording_id: int | None, error: str) -> None:
        self._mark_recording(recording_id, "failed", error)

    def _mark_recording(
        self, recording_id: int | None, status: str, error: str | None
    ) -> None:
        if recording_id is None:
            return

        with closing(connect(self.database_url)) as conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE voice_recordings
                    SET status = %s,
                        error = %s,
                        updated_at = NOW()
                    WHERE id = %s AND status NOT IN ('completed', 'discarded_no_credits', 'discarded_privacy')
                    """,
                    (status, error, recording_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def claim_session_agent_run(self, session_id: int) -> bool:
        with closing(connect(self.database_url)) as conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE voice_sessions
                    SET status = 'agent_running',
                        agent_error = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                      AND status IN ('finished', 'agent_running')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM voice_recordings
                          WHERE session_id = voice_sessions.id
                            AND status IN ('transcribing', 'pending', 'streaming', 'fallback_pending')
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
        with closing(connect(self.database_url)) as conn:
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
                if status == "agent_done":
                    cur.execute(
                        "INSERT INTO voice_profile_jobs (session_id, user_id) "
                        "SELECT DISTINCT session_id, user_id FROM messages WHERE session_id = %s "
                        "ON CONFLICT DO NOTHING",
                        (session_id,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_voice_profile_jobs(self) -> list[tuple[int, int]]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT pj.session_id, pj.user_id FROM voice_profile_jobs pj JOIN voice_sessions vs ON vs.id = pj.session_id "
                "WHERE pj.status = 'pending' AND vs.status = 'agent_done' "
                "AND NOT EXISTS (SELECT 1 FROM voice_recordings vr WHERE vr.session_id = vs.id "
                "AND vr.status IN ('pending', 'transcribing', 'streaming', 'fallback_pending')) ORDER BY pj.session_id, pj.user_id LIMIT 100"
            )
            return [(int(row[0]), int(row[1])) for row in cur.fetchall()]

    def get_voice_profile_job_revision(
        self, session_id: int, user_id: int
    ) -> int | None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT pj.revision FROM voice_profile_jobs pj JOIN voice_sessions vs ON vs.id = pj.session_id "
                "WHERE pj.session_id = %s AND pj.user_id = %s AND pj.status = 'pending' AND vs.status = 'agent_done'",
                (session_id, user_id),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None

    def mark_voice_profile_job(
        self,
        session_id: int,
        user_id: int,
        status: str,
        error: str | None = None,
        expected_revision: int | None = None,
    ) -> None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE voice_profile_jobs SET status = %s, error = %s WHERE session_id = %s AND user_id = %s "
                "AND (%s::int IS NULL OR revision = %s)",
                (
                    status,
                    error,
                    session_id,
                    user_id,
                    expected_revision,
                    expected_revision,
                ),
            )
            conn.commit()

    def get_session_messages(self, session_id: int) -> list[dict]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT m.tstamp, u.discord_id, u.username, u.display_name, m.channel_name, m.content,
                       m.id, m.recording_id
                FROM messages m
                JOIN users u ON u.id = m.user_id
                WHERE m.session_id = %s AND m.source_type = 'voice'
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
                    "id": row[6],
                    "recording_id": row[7],
                }
                for row in cur.fetchall()
            ]

    def get_session_participants(self, session_id: int) -> list[SessionParticipant]:
        with closing(connect(self.database_url)) as conn:
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

    def get_user_profile_by_discord_id(self, discord_id: str) -> UserProfile | None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.id, u.discord_id, u.username, u.display_name,
                       COALESCE(p.anthropologist_title, ''),
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

    def get_user_profile_by_user_id(self, user_id: int) -> UserProfile | None:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.id, u.discord_id, u.username, u.display_name,
                       COALESCE(p.anthropologist_title, ''),
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

    def upsert_user_profile(
        self,
        *,
        user_id: int,
        anthropologist_title: str,
        summary: str,
        interests: str,
        communication_style: str,
        known_facts: str,
        recent_updates: str,
        google_doc_id: str | None,
        google_doc_url: str | None,
    ) -> None:
        with closing(connect(self.database_url)) as conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO user_profiles (
                        user_id, anthropologist_title, summary, interests, communication_style, known_facts,
                        recent_updates, google_doc_id, google_doc_url, last_updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET anthropologist_title = EXCLUDED.anthropologist_title,
                        summary = EXCLUDED.summary,
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
                        anthropologist_title.strip(),
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

    def get_pending_text_profiles(self) -> list[PendingTextProfile]:
        with closing(connect(self.database_url)) as conn:
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

    def get_text_messages_for_profile(
        self, user_id: int, after: datetime | None, until: datetime | None = None
    ) -> list[dict]:
        with closing(connect(self.database_url)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT m.tstamp, u.discord_id, u.username, u.display_name, m.channel_name, m.content "
                "FROM messages m JOIN users u ON u.id = m.user_id "
                "WHERE m.user_id = %s AND m.source_type = 'text' AND m.content <> '' "
                "AND (%s::timestamptz IS NULL OR m.tstamp > %s) "
                "AND (%s::timestamptz IS NULL OR m.tstamp <= %s) ORDER BY m.tstamp ASC, m.id ASC",
                (user_id, after, after, until, until),
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

    def mark_user_text_profile_seen(self, user_id: int, seen_at: datetime) -> None:
        with closing(connect(self.database_url)) as conn:
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


    def streaming_preference(self, guild_id: str, default: bool) -> bool:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT streaming_enabled FROM guild_transcription_settings WHERE guild_id = %s",
                    (guild_id,),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] is not None else default

    def set_streaming_preference(self, guild_id: str, enabled: bool) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO guild_transcription_settings(guild_id, streaming_enabled) VALUES (%s, %s) ON CONFLICT (guild_id) DO UPDATE SET streaming_enabled = EXCLUDED.streaming_enabled, updated_at = NOW()",
                    (guild_id, enabled),
                )
            conn.commit()

    def assistant_settings(self, guild_id: str) -> dict:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT assistant_enabled, assistant_phrase, assistant_channel_id, assistant_revision "
                    "FROM guild_transcription_settings WHERE guild_id = %s",
                    (guild_id,),
                )
                row = cur.fetchone() or (True, "Olá macaco", None, 0)
        return dict(zip(("enabled", "phrase", "channel_id", "revision"), row))

    def update_assistant_settings(self, guild_id: str, changes: dict) -> dict:
        # Column names are allowlisted here; values are always bound parameters.
        columns = {"enabled": "assistant_enabled", "phrase": "assistant_phrase",
                   "channel_id": "assistant_channel_id"}
        if not changes or not changes.keys() <= columns.keys():
            raise ValueError("Invalid assistant settings")
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO guild_transcription_settings(guild_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (guild_id,),
                )
                assignments = ", ".join(columns[key] + " = %s" for key in changes)
                cur.execute(
                    "UPDATE guild_transcription_settings SET " + assignments +
                    ", assistant_revision = assistant_revision + 1, updated_at = NOW() "
                    "WHERE guild_id = %s RETURNING assistant_enabled, assistant_phrase, "
                    "assistant_channel_id, assistant_revision",
                    (*changes.values(), guild_id),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(zip(("enabled", "phrase", "channel_id", "revision"), row))

    def start_realtime_unit(
        self, session_id: int, filename: str, metadata: dict, token: str, key_name: str
    ) -> tuple[int, int]:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, credits_exhausted FROM voice_sessions WHERE id = %s FOR UPDATE",
                    (session_id,),
                )
                if cur.fetchone() != ("open", False):
                    raise ValueError("Session is closed or suspended")
                cur.execute(
                    "INSERT INTO voice_recordings(session_id, recording_filename, discord_id, status, metadata, stream_token, realtime_key_name) VALUES (%s, %s, %s, 'streaming', %s::jsonb, %s, %s) ON CONFLICT DO NOTHING RETURNING id, generation",
                    (
                        session_id,
                        filename,
                        metadata["discord_id"],
                        json.dumps(metadata),
                        token,
                        key_name,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(
                        "Audio unit already admitted; recover through Batch"
                    )
            conn.commit()
            return row

    def insert_realtime_final(
        self,
        recording_id: int,
        generation: int,
        identity: str,
        text: str,
        timestamp: datetime,
    ) -> bool:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id, metadata FROM voice_recordings WHERE id = %s AND generation = %s AND status = 'streaming' FOR UPDATE",
                    (recording_id, generation),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                session_id, meta = row
                cur.execute(
                    "INSERT INTO realtime_finals VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING identity",
                    (recording_id, generation, identity),
                )
                if cur.fetchone() is None:
                    return False
                if text.strip():
                    user = self._upsert_user(
                        conn,
                        meta["discord_id"],
                        meta["username"],
                        meta.get("display_name"),
                    )
                    ids = self._insert_messages(
                        conn,
                        user,
                        session_id,
                        meta["channel_name"],
                        [MessageInsert(text.strip(), normalize_timestamp(timestamp))],
                    )
                    cur.execute(
                        "UPDATE messages SET recording_id = %s WHERE id = ANY(%s)",
                        (recording_id, ids),
                    )
                    self._rebuild_chunks(conn, meta["channel_name"], [timestamp])
            conn.commit()
            return True

    def checkpoint_realtime_usage(
        self, recording_id: int, generation: int, seconds: float
    ) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_recordings SET realtime_seconds = GREATEST(realtime_seconds, %s) "
                    "WHERE id = %s AND generation = %s AND status = 'streaming'",
                    (seconds, recording_id, generation),
                )
            conn.commit()

    def finish_realtime_unit(
        self, recording_id: int, generation: int, seconds: float, completed: bool
    ) -> bool:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_recordings SET status = %s, realtime_seconds = GREATEST(realtime_seconds, %s), generation = generation + 1, completed_at = CASE WHEN %s THEN NOW() ELSE NULL END, updated_at = NOW() WHERE id = %s AND generation = %s AND status = 'streaming' RETURNING id",
                    (
                        "completed" if completed else "fallback_pending",
                        seconds,
                        completed,
                        recording_id,
                        generation,
                    ),
                )
                changed = cur.fetchone() is not None
                cur.execute(
                    "UPDATE voice_recordings SET realtime_seconds = GREATEST(realtime_seconds, %s) WHERE id = %s",
                    (seconds, recording_id),
                )
            conn.commit()
            return changed

    def recording_is_live(self, recording_id: int) -> bool:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM voice_recordings WHERE id = %s", (recording_id,)
                )
                row = cur.fetchone()
                return row is not None and row[0] in {
                    "pending",
                    "transcribing",
                    "streaming",
                }

    def invalidate_user_recordings(self, discord_id: str) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_recordings SET status = 'discarded_privacy', generation = generation + 1 WHERE discord_id = %s AND status NOT IN ('completed', 'discarded_no_credits', 'discarded_privacy')",
                    (discord_id,),
                )
            conn.commit()

    def recoverable_realtime_units(self) -> list[tuple[int, str]]:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, recording_filename FROM voice_recordings WHERE status = 'fallback_pending'"
                )
                return cur.fetchall()

    def admit_realtime_recovery(self, recording_id: int) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_recordings SET status = 'pending', generation = generation + 1 WHERE id = %s AND status = 'fallback_pending'",
                    (recording_id,),
                )
            conn.commit()

    def recover_realtime_units(self) -> None:
        # Bot outbox closes/repairs WAVs before Batch admission. Never read an open WAV.
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_recordings SET status = 'fallback_pending', generation = generation + 1 WHERE status = 'streaming'"
                )
            conn.commit()

    def discard_session_audio(self, session_id: int) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_sessions SET credits_episode = credits_episode + CASE WHEN credits_exhausted THEN 0 ELSE 1 END, credits_exhausted = TRUE WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE voice_recordings SET status = 'discarded_no_credits', generation = generation + 1, cleanup_pending = TRUE, updated_at = NOW() WHERE session_id = %s AND status NOT IN ('completed', 'discarded_no_credits', 'discarded_privacy')",
                    (session_id,),
                )
            conn.commit()

    def session_credit_state(self, session_id: int) -> dict:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT credits_exhausted, credits_notice_sent, credits_episode, EXISTS(SELECT 1 FROM voice_recordings WHERE session_id = %s AND cleanup_pending) FROM voice_sessions WHERE id = %s",
                    (session_id, session_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError("Session does not exist")
                return dict(
                    zip(("exhausted", "notice_sent", "episode", "cleanup_pending"), row)
                )

    def reset_session_credits(self, session_id: int) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_sessions SET credits_exhausted = FALSE, credits_notice_sent = FALSE WHERE id = %s",
                    (session_id,),
                )
            conn.commit()

    def acknowledge_credit_notice(self, session_id: int, episode: int) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_sessions SET credits_notice_sent = TRUE WHERE id = %s AND credits_exhausted AND credits_episode = %s",
                    (session_id, episode),
                )
            conn.commit()

    def pending_credit_notices(self) -> list[dict]:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, guild_id, summary_channel_id, credits_episode, EXISTS(SELECT 1 FROM voice_recordings vr WHERE vr.session_id = vs.id AND cleanup_pending) FROM voice_sessions vs WHERE credits_exhausted AND NOT credits_notice_sent AND summary_channel_id IS NOT NULL ORDER BY id LIMIT 100"
                )
                return [
                    dict(
                        zip(
                            (
                                "session_id",
                                "guild_id",
                                "channel_id",
                                "episode",
                                "cleanup_pending",
                            ),
                            row,
                        )
                    )
                    for row in cur.fetchall()
                ]

    def discarded_cleanup_jobs(self) -> list[tuple[int, str]]:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, recording_filename FROM voice_recordings WHERE status = 'discarded_no_credits' AND cleanup_pending"
                )
                return cur.fetchall()

    def acknowledge_discard_cleanup(self, recording_id: int) -> None:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE voice_recordings SET cleanup_pending = FALSE WHERE id = %s AND status = 'discarded_no_credits'",
                    (recording_id,),
                )
            conn.commit()

    def local_realtime_hours(self) -> dict[str, float]:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT realtime_key_name, SUM(realtime_seconds) / 3600 FROM voice_recordings WHERE realtime_key_name IS NOT NULL GROUP BY realtime_key_name"
                )
                return dict(cur.fetchall())

    def local_realtime_usage(self, *, since: str) -> list[dict]:
        with closing(connect(self.database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT realtime_key_name, COALESCE(metadata->>'speechmatics_realtime_model', 'unknown'), "
                    "SUM(realtime_seconds) / 3600 FROM voice_recordings "
                    "WHERE realtime_key_name IS NOT NULL AND created_at >= %s::date AT TIME ZONE 'UTC' "
                    "AND created_at < (date_trunc('day', NOW() AT TIME ZONE 'UTC') + INTERVAL '1 day') AT TIME ZONE 'UTC' "
                    "GROUP BY realtime_key_name, metadata->>'speechmatics_realtime_model'",
                    (since,),
                )
                return [
                    dict(key_name=key, model=model, used_hours=float(hours))
                    for key, model, hours in cur.fetchall()
                ]

def connect(database_url: str):
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("DATABASE_URL must use postgresql://")
    # libpq handles percent-encoded passwords and connection options correctly.
    return psycopg2.connect(
        database_url, connect_timeout=5, application_name="discord_anthropologist"
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
        response_language=row[10] if len(row) > 10 else "pt",
    )


def user_profile_from_row(row) -> UserProfile:
    return UserProfile(
        user_id=int(row[0]),
        discord_id=row[1],
        username=row[2],
        display_name=row[3],
        anthropologist_title=row[4],
        summary=row[5],
        interests=row[6],
        communication_style=row[7],
        known_facts=row[8],
        recent_updates=row[9],
        google_doc_id=row[10],
        google_doc_url=row[11],
        last_updated_at=row[12],
        last_text_seen_at=row[13],
    )
