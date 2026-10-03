"""Persistent time windows and publication claims for the group's living memory."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta

from .repository import connect


def memory_guilds(
    repository, now: datetime, minutes: int, context_bulks: int
) -> list[str]:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT COALESCE(m.guild_id, vs.guild_id) FROM messages m "
                "LEFT JOIN voice_sessions vs ON vs.id = m.session_id "
                "WHERE COALESCE(m.guild_id, vs.guild_id) IS NOT NULL"
            )
            guilds = [row[0] for row in cur.fetchall()]
            for guild in guilds:
                cur.execute(
                    "INSERT INTO group_memory_state(guild_id, started_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (guild, now - timedelta(minutes=minutes * context_bulks)),
                )
        conn.commit()
        return guilds


def next_bulk_messages(
    repository, guild_id: str, now: datetime, minutes: int
) -> tuple[datetime | None, list[dict]]:
    seconds = minutes * 60
    closed_at = datetime.fromtimestamp(
        int(now.timestamp()) // seconds * seconds, now.tzinfo
    )
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(m.observed_at) FROM messages m "
                "LEFT JOIN voice_sessions vs ON vs.id = m.session_id "
                "JOIN group_memory_state st ON st.guild_id = COALESCE(m.guild_id, vs.guild_id) "
                "LEFT JOIN group_memory_messages gm ON gm.message_id = m.id "
                "WHERE st.guild_id = %s AND m.observed_at >= st.started_at "
                "AND m.observed_at < %s AND gm.message_id IS NULL AND m.content <> ''",
                (guild_id, closed_at),
            )
            first = cur.fetchone()[0]
            if first is None:
                return None, []
            start = datetime.fromtimestamp(
                int(first.timestamp()) // seconds * seconds, now.tzinfo
            )
            cur.execute(
                "SELECT m.id, m.user_id, u.discord_id, u.username, u.display_name, m.tstamp, "
                "m.channel_name, m.content, m.source_type, m.assistant_answer, "
                "CASE WHEN m.source_type = 'text' THEN m.channel_id ELSE vs.summary_channel_id END "
                "FROM messages m JOIN users u ON u.id = m.user_id "
                "LEFT JOIN voice_sessions vs ON vs.id = m.session_id "
                "LEFT JOIN group_memory_messages gm ON gm.message_id = m.id "
                "JOIN group_memory_state st ON st.guild_id = COALESCE(m.guild_id, vs.guild_id) "
                "WHERE st.guild_id = %s AND m.observed_at >= GREATEST(%s, st.started_at) "
                "AND m.observed_at < %s AND gm.message_id IS NULL AND m.content <> '' "
                "ORDER BY m.tstamp, m.id",
                (guild_id, start, start + timedelta(seconds=seconds)),
            )
            keys = (
                "id",
                "user_id",
                "discord_id",
                "username",
                "display_name",
                "tstamp",
                "channel_name",
                "content",
                "source_type",
                "assistant_answer",
                "destination",
            )
            return start, [dict(zip(keys, row)) for row in cur.fetchall()]


def recent_bulks(repository, guild_id: str, limit: int = 3) -> list[dict]:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, start_at, end_at, summary, lore, reaction_text, created_at FROM group_memory_bulks "
                "WHERE guild_id = %s ORDER BY end_at DESC LIMIT %s",
                (guild_id, limit),
            )
            return [
                dict(
                    zip(
                        (
                            "id",
                            "start_at",
                            "end_at",
                            "summary",
                            "lore",
                            "reaction_text",
                            "created_at",
                        ),
                        row,
                    )
                )
                for row in cur.fetchall()
            ]


def save_bulk(
    repository,
    guild_id: str,
    start: datetime,
    end: datetime,
    messages: list[dict],
    generated: dict,
    channel_id: str | None,
) -> int:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            # Hold source rows until the memory and its provenance commit together.
            ids = [message["id"] for message in messages]
            cur.execute("SELECT id FROM messages WHERE id = ANY(%s) FOR SHARE", (ids,))
            if len(cur.fetchall()) != len(ids):
                raise ValueError("Bulk input was removed while memory was generated")
            cur.execute(
                "INSERT INTO group_memory_bulks (guild_id, start_at, end_at, summary, lore, channel_id, reaction_text, speak, gif_query, reaction_status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(guild_id,start_at,end_at) DO UPDATE SET summary = EXCLUDED.summary, lore = EXCLUDED.lore "
                "RETURNING id",
                (
                    guild_id,
                    start,
                    end,
                    generated["summary"],
                    generated["lore"],
                    channel_id,
                    generated["reaction_text"],
                    generated["speak"],
                    generated["gif_query"],
                    "pending"
                    if generated["reaction_text"] and channel_id
                    else "silent",
                ),
            )
            bulk_id = cur.fetchone()[0]
            cur.executemany(
                "INSERT INTO group_memory_messages(bulk_id,message_id) VALUES (%s,%s)",
                [(bulk_id, message_id) for message_id in ids],
            )
            cur.execute(
                "UPDATE group_memory_bulks SET profiled_user_ids = ARRAY(SELECT unnest(profiled_user_ids) EXCEPT SELECT unnest(%s::bigint[])), "
                "member_ids = ARRAY(SELECT unnest(member_ids) UNION SELECT unnest(%s::bigint[])) WHERE id = %s",
                (
                    list({m["user_id"] for m in messages}),
                    list({m["user_id"] for m in messages}),
                    bulk_id,
                ),
            )
        conn.commit()
        return bulk_id


def profile_jobs(repository, guild_id: str) -> list[tuple[int, int, list[dict]]]:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT b.id, m.user_id, m.id, u.discord_id, u.username, u.display_name, "
                "m.tstamp, m.channel_name, m.content, m.source_type, m.assistant_answer "
                "FROM group_memory_bulks b JOIN group_memory_messages gm ON gm.bulk_id = b.id "
                "JOIN messages m ON m.id = gm.message_id JOIN users u ON u.id = m.user_id "
                "WHERE b.guild_id = %s AND NOT (m.user_id = ANY(b.profiled_user_ids)) ORDER BY b.id,m.user_id,m.tstamp,m.id",
                (guild_id,),
            )
            jobs = {}
            keys = (
                "id",
                "discord_id",
                "username",
                "display_name",
                "tstamp",
                "channel_name",
                "content",
                "source_type",
                "assistant_answer",
            )
            for row in cur.fetchall():
                jobs.setdefault((row[0], row[1]), []).append(dict(zip(keys, row[2:])))
            return [
                (bulk_id, user_id, messages)
                for (bulk_id, user_id), messages in jobs.items()
            ]


def mark_profile_done(repository, bulk_id: int, user_id: int) -> None:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE group_memory_bulks SET profiled_user_ids = array_append(profiled_user_ids,%s) WHERE id = %s AND NOT (%s = ANY(profiled_user_ids))",
                (user_id, bulk_id, user_id),
            )
        conn.commit()


def pending_reactions(repository, now: datetime, max_age_minutes: int) -> list[dict]:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT b.id,b.guild_id,b.channel_id,b.reaction_text,b.speak,b.gif_query FROM group_memory_bulks b "
                "LEFT JOIN guild_transcription_settings s ON s.guild_id = b.guild_id "
                "WHERE b.reaction_status = 'pending' AND b.end_at >= %s AND COALESCE(s.assistant_enabled, TRUE) "
                "ORDER BY b.id LIMIT 20",
                (now - timedelta(minutes=max_age_minutes),),
            )
            return [
                dict(
                    zip(
                        ("id", "guild_id", "channel_id", "text", "speak", "gif_query"),
                        row,
                    )
                )
                for row in cur.fetchall()
            ]


def claim_reaction(
    repository, bulk_id: int, now: datetime, max_age_minutes: int
) -> bool:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE group_memory_bulks b SET reaction_status = 'claimed' WHERE b.id = %s "
                "AND b.reaction_status = 'pending' AND b.end_at >= %s AND NOT EXISTS ("
                "SELECT 1 FROM guild_transcription_settings s WHERE s.guild_id = b.guild_id AND NOT s.assistant_enabled) RETURNING b.id",
                (bulk_id, now - timedelta(minutes=max_age_minutes)),
            )
            claimed = cur.fetchone() is not None
        conn.commit()
        return claimed


def finish_reaction(repository, bulk_id: int, status: str) -> None:
    with closing(connect(repository.database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE group_memory_bulks SET reaction_status = %s WHERE id = %s AND reaction_status = 'claimed'",
                (status, bulk_id),
            )
        conn.commit()
