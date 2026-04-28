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
            message_ids = self._insert_messages(conn, user_id, channel_name, normalized_messages)
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
        channel_name: str,
        messages: list[MessageInsert],
    ) -> list[int]:
        ids: list[int] = []
        cur = conn.cursor()
        for message in messages:
            cur.execute(
                """
                INSERT INTO messages (user_id, source_type, channel_name, content, tstamp)
                VALUES (%s, 'voice', %s, %s, %s)
                RETURNING id
                """,
                (user_id, channel_name, message.content, message.tstamp),
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
