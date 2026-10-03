"""Single-process reservations and the internal PCM → Speechmatics bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import struct
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

from .recording_cleanup import RecordingCleanup
from .speechmatics_errors import (
    NoCredits,
    ProviderError,
    error_kind,
    key_health,
    message_error,
)

logger = logging.getLogger("uvicorn.error")


@dataclass
class Reservation:
    discord_id: str
    key: object
    token: str
    active: bool = False
    retiring: bool = False
    retire_at: float = 0
    retry_at: float = 0


class RealtimePool:
    """All methods run on the API event loop; no await between admission and mutation."""

    def __init__(self, keys):
        self.keys = tuple({key.value: key for key in reversed(keys)}.values())[::-1]
        self.session_id: int | None = None
        self.reservations: dict[str, Reservation] = {}
        self.last_roster = 0.0

    def reconcile(
        self, session_id: int, users: list[str], enabled: bool
    ) -> dict[str, dict]:
        now = time.monotonic()
        # A dead bot cannot hold the pool forever. Live streams still count until closed.
        if self.session_id != session_id and now - self.last_roster > 15:
            for token, reservation in list(self.reservations.items()):
                if not reservation.active:
                    del self.reservations[token]
                else:
                    reservation.retiring = True
                    reservation.retire_at = now + 5
        if self.session_id not in {None, session_id} and self.reservations:
            logger.info("Realtime refused second call session=%s", session_id)
            return {}
        self.session_id = session_id
        self.last_roster = now
        eligible = set(users) if enabled else set()
        for token, reservation in list(self.reservations.items()):
            if reservation.discord_id not in eligible:
                if not reservation.retiring:
                    reservation.retire_at = now + 5
                reservation.retiring = True
                if not reservation.active:
                    del self.reservations[token]
        for user in users if enabled else []:
            if any(
                r.discord_id == user and not r.retiring
                for r in self.reservations.values()
            ):
                continue
            available = [
                key
                for key in self.keys
                if key_health.healthy(key.value) and self.occupancy(key) < 2
            ]
            if not available:
                break  # FIFO, no overtaking while waiting.
            key = min(available, key=lambda k: (self.occupancy(k), self.keys.index(k)))
            reservation = Reservation(user, key, str(uuid.uuid4()))
            self.reservations[reservation.token] = reservation
            logger.info(
                "Realtime assigned session=%s user=%s key=%s slot=%s",
                session_id,
                user,
                key.name,
                self.occupancy(key),
            )
        return {
            r.discord_id: {"token": r.token, "key_name": r.key.name}
            for r in self.reservations.values()
            if not r.retiring and r.retry_at <= now
        }

    def occupancy(self, key) -> int:
        return sum(r.key.value == key.value for r in self.reservations.values())

    def acquire(self, session_id: int, user: str, token: str) -> Reservation:
        r = self.reservations.get(token)
        if (
            self.session_id != session_id
            or r is None
            or r.discord_id != user
            or r.active
            or r.retiring
        ):
            raise ProviderError("unreserved")
        r.active = True
        return r

    def release_epoch(self, r: Reservation) -> None:
        r.active = False
        if r.retiring:
            self.reservations.pop(r.token, None)

    def alternate(self, r: Reservation, attempted: set[str]) -> bool:
        available = [
            k
            for k in self.keys
            if k.value not in attempted
            and key_health.healthy(k.value)
            and self.occupancy(k) < 2
        ]
        if not available:
            return False
        r.key = min(available, key=lambda k: (self.occupancy(k), self.keys.index(k)))
        return True


def valid_configuration(settings, keys) -> bool:
    return (
        settings.transcription_provider == "speechmatics"
        and bool(keys)
        and settings.speechmatics_realtime_model in {"enhanced", "standard"}
        and settings.speechmatics_realtime_language == "pt"
    )


async def open_provider(settings, pool, reservation):
    attempted: set[str] = set()
    while True:
        key = reservation.key
        attempted.add(key.value)
        upstream = None
        try:
            if not key_health.healthy(key.value):
                raise ProviderError(
                    "no_credits" if key_health.exhausted([key]) else "invalid_key"
                )
            upstream = await connect(
                settings.speechmatics_realtime_url,
                additional_headers={"Authorization": "Bearer " + key.value},
                open_timeout=10,
                close_timeout=2,
                ping_interval=15,
                ping_timeout=15,
                max_size=1 << 20,
                max_queue=16,
                write_limit=65536,
            )
            await upstream.send(
                json.dumps(
                    {
                        "message": "StartRecognition",
                        "audio_format": {
                            "type": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": 48000,
                        },
                        "transcription_config": {
                            "language": settings.speechmatics_realtime_language,
                            "model": settings.speechmatics_realtime_model,
                            "enable_partials": True,
                        },
                    }
                )
            )
            async with asyncio.timeout(10):
                while True:
                    event = json.loads(await upstream.recv())
                    if event.get("message") == "RecognitionStarted":
                        return upstream
                    if event.get("message") == "Error":
                        raise message_error(event)
        except Exception as exc:
            if upstream is not None:
                await upstream.close()
            kind = error_kind(exc)
            key_health.mark(key.value, kind)
            logger.warning(
                "Realtime start failed user=%s key=%s reason=%s",
                reservation.discord_id,
                key.name,
                kind,
            )
            if kind in {"no_credits", "capacity", "invalid_key"} and pool.alternate(
                reservation, attempted
            ):
                continue
            if key_health.exhausted(pool.keys):
                raise NoCredits() from None
            reservation.retry_at = time.monotonic() + 10
            raise ProviderError(kind) from None


def final_words(results: list[dict]) -> list[dict]:
    words = []
    for item in results:
        alternatives = item.get("alternatives") or []
        if not alternatives:
            continue
        text = alternatives[0]["content"]
        if item.get("type") in {"word", "entity"}:
            words.append(
                {"text": text, "start": item["start_time"], "end": item["end_time"]}
            )
        elif item.get("type") == "punctuation" and words:
            words[-1]["text"] += text
    return words


async def bridge(
    websocket: WebSocket, *, settings, repository, pool, validate_filename, resolve_path
):
    await websocket.accept()
    reservation = upstream = None
    recording_id = None
    generation = 0
    frames = packets = 0
    sent_frames = 0
    completed = False
    session_id = None
    tasks = []
    try:
        async with asyncio.timeout(10):
            meta = await websocket.receive_json()
        session_id = int(meta["session_id"])
        user = str(meta["discord_id"])
        filename = meta["recording_filename"]
        validate_filename(filename)
        path = resolve_path(filename, settings)
        started_at = datetime.fromisoformat(
            meta["recording_started_at"].replace("Z", "+00:00")
        )
        if (
            not user
            or not meta.get("username")
            or not meta.get("channel_name")
            or started_at.tzinfo is None
        ):
            raise ValueError("Invalid unit metadata")
        # Rotation drains the previous WAV epoch while capture buffers the next one.
        async with asyncio.timeout(10):
            while True:
                existing = pool.reservations.get(meta["token"])
                if existing is None or not existing.active:
                    break
                await asyncio.sleep(0.05)
        reservation = pool.acquire(session_id, user, meta["token"])
        upstream = await open_provider(settings, pool, reservation)
        recording_id, generation = await asyncio.to_thread(
            repository.start_realtime_unit,
            session_id,
            filename,
            {
                **meta,
                "speechmatics_realtime_model": settings.speechmatics_realtime_model.strip().lower(),
            },
            reservation.token,
            reservation.key.name,
        )
        await websocket.send_json({"type": "ready", "generation": generation, "recording_id": recording_id})
        eos = asyncio.Event()

        async def send_audio():
            nonlocal frames, packets, sent_frames
            last_checkpoint = 0.0
            while True:
                packet = await websocket.receive()
                if packet["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect()
                data = packet.get("bytes")
                if data is None:
                    control = json.loads(packet.get("text") or "{}")
                    if (
                        control.get("type") != "end"
                        or control.get("last_seq_no") != packets
                    ):
                        raise ValueError("Invalid end boundary")
                    with wave.open(str(path), "rb") as wav:
                        if (
                            wav.getframerate() != 48000
                            or wav.getnchannels() != 2
                            or wav.getsampwidth() != 2
                            or wav.getnframes() != frames
                        ):
                            raise ValueError("Safety WAV does not match sent audio")
                    eos.set()
                    await upstream.send(
                        json.dumps({"message": "EndOfStream", "last_seq_no": packets})
                    )
                    return
                if len(data) < 10 or len(data) > 8 + 48000 * 2 or (len(data) - 8) % 2:
                    raise ValueError("Invalid PCM frame")
                sequence = struct.unpack("<Q", data[:8])[0]
                if sequence != packets + 1:
                    raise ValueError("Duplicate or out-of-order PCM")
                packets += 1
                frames += (len(data) - 8) // 2
                await upstream.send(data[8:])
                sent_frames += (len(data) - 8) // 2
                if time.monotonic() - last_checkpoint >= 5:
                    await asyncio.to_thread(
                        repository.checkpoint_realtime_usage,
                        recording_id,
                        generation,
                        sent_frames / 48000,
                    )
                    last_checkpoint = time.monotonic()

        async def receive_results():
            while True:
                event = json.loads(await upstream.recv())
                kind = event.get("message")
                if kind == "Error":
                    raise message_error(event)
                if kind == "Warning" and event.get("type") == "duration_limit_exceeded":
                    raise ProviderError("recoverable")
                if kind in {"AddPartialTranscript", "AddTranscript"}:
                    result = event["metadata"]
                    start, end = float(result["start_time"]), float(result["end_time"])
                    if (
                        not math.isfinite(start)
                        or not math.isfinite(end)
                        or start < 0
                        or end < start
                        or end > frames / 48000 + 0.1
                    ):
                        raise ValueError("Invalid provider timestamp")
                    if settings.transcription_streaming_debug:
                        logger.info(
                            "Realtime debug session=%s user=%s type=%s text=%s",
                            session_id,
                            user,
                            kind,
                            result["transcript"],
                        )
                    if kind == "AddPartialTranscript" and result["transcript"].strip():
                        # Timing only: unfinished text must never activate or reach the LLM.
                        await websocket.send_json(
                            {
                                "type": "speech",
                                "session_id": session_id,
                                "discord_id": user,
                                "recording_id": recording_id,
                                "generation": generation,
                                "start": start,
                                "end": end,
                            }
                        )
                    if kind == "AddTranscript":
                        identity = hashlib.sha256(
                            json.dumps(result, sort_keys=True).encode()
                        ).hexdigest()
                        inserted = await asyncio.to_thread(
                            repository.insert_realtime_final,
                            recording_id,
                            generation,
                            identity,
                            result["transcript"],
                            started_at + timedelta(seconds=start),
                        )
                        if inserted:
                            # Only new committed finals are delivered; Batch never uses this channel.
                            await websocket.send_json(
                                {
                                    "type": "final",
                                    "session_id": session_id,
                                    "discord_id": user,
                                    "recording_id": recording_id,
                                    "generation": generation,
                                    "identity": identity,
                                    "start": start,
                                    "end": end,
                                    "text": result["transcript"],
                                    "words": final_words(event.get("results", [])),
                                }
                            )
                if kind == "EndOfTranscript":
                    if not eos.is_set():
                        raise ProviderError("recoverable")
                    return

        async def monitor():
            while True:
                await asyncio.sleep(1)
                state = await asyncio.to_thread(
                    repository.session_credit_state, session_id
                )
                if state["exhausted"]:
                    raise NoCredits()
                if not await asyncio.to_thread(
                    repository.recording_is_live, recording_id
                ):
                    raise ProviderError("revoked")
                if (
                    reservation.retiring
                    and time.monotonic() >= reservation.retire_at
                    and not eos.is_set()
                ) or time.monotonic() - pool.last_roster > 15:
                    raise ProviderError("revoked")

        sender = asyncio.create_task(send_audio())
        receiver = asyncio.create_task(receive_results())
        watcher = asyncio.create_task(monitor())
        tasks = [sender, receiver, watcher]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if sender not in done:
            raise ProviderError("recoverable")
        done, _ = await asyncio.wait(
            [receiver, watcher], timeout=20, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise TimeoutError("Realtime flush timed out")
        for task in done:
            task.result()
        if receiver not in done:
            raise ProviderError("recoverable")
        completed = await asyncio.to_thread(
            repository.finish_realtime_unit,
            recording_id,
            generation,
            sent_frames / 48000,
            True,
        )
        if not completed:
            raise ProviderError("revoked")
        await websocket.send_json({"type": "completed"})
    except Exception as exc:
        kind = error_kind(exc)
        if reservation is not None:
            key_health.mark(reservation.key.value, kind)
            reservation.retry_at = time.monotonic() + 10
        if session_id is not None and key_health.exhausted(pool.keys):
            await asyncio.to_thread(repository.discard_session_audio, session_id)
            kind = "no_credits"
        logger.warning(
            "Realtime fallback session=%s unit=%s reason=%s",
            session_id,
            recording_id,
            kind,
        )
        try:
            await websocket.send_json({"type": "fallback", "reason": kind})
        except Exception:
            pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if recording_id is not None and not completed:
            await asyncio.to_thread(
                repository.finish_realtime_unit,
                recording_id,
                generation,
                sent_frames / 48000,
                False,
            )
        if upstream is not None:
            await upstream.close()
        if reservation is not None:
            pool.release_epoch(reservation)
        if completed:
            await asyncio.to_thread(
                RecordingCleanup(
                    repository=repository, settings=settings
                ).cleanup_completed_file,
                filename,
            )
        try:
            await websocket.close()
        except Exception:
            pass
