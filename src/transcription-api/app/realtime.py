"""Single-process reservations and the internal PCM → provider bridge."""

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
from websockets.exceptions import ConnectionClosedOK

from .deepgram import (
    deepgram_error,
    deepgram_message_error,
    normalize_event,
    streaming_url,
    validate_interval,
)
from .providers import ProviderKey, ProviderRegistry, effective_order, environment_order
from .recording_cleanup import RecordingCleanup
from .speechmatics_errors import (
    NoCredits,
    ProviderError,
    error_kind,
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
    order: tuple[str, ...] = ("speechmatics",)


class RealtimePool:
    """All methods run on the API event loop; no await between admission and mutation."""

    def __init__(self, keys, registry=None):
        self.registry = registry or ProviderRegistry(keys=tuple(
            ProviderKey(k.name, k.value, group=k.name) for k in
            tuple({key.value: key for key in reversed(keys)}.values())[::-1]))
        self.keys = self.registry.keys
        self.order = ("speechmatics",)
        self.session_id: int | None = None
        self.reservations: dict[str, Reservation] = {}
        self.last_roster = 0.0

    def reconcile(
        self, session_id: int, users: list[str], enabled: bool, order=None
    ) -> dict[str, dict]:
        now = time.monotonic()
        if order is not None:
            self.order = tuple(order)
        # A dead bot cannot hold the pool forever. Live streams still count until closed.
        if self.session_id != session_id and now - self.last_roster > 15:
            for token, reservation in list(self.reservations.items()):
                if not reservation.active:
                    self.registry.release(token)
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
                    self.registry.release(token)
                    del self.reservations[token]
        for user in users if enabled else []:
            if any(
                r.discord_id == user and not r.retiring
                for r in self.reservations.values()
            ):
                continue
            token = str(uuid.uuid4())
            key = self.registry.reserve(token, self.order, "streaming")
            if key is None:
                break  # FIFO, no overtaking while waiting.
            reservation = Reservation(user, key, token, order=self.order)
            self.reservations[reservation.token] = reservation
            logger.info(
                "Realtime assigned session=%s user=%s key=%s slot=%s",
                session_id,
                user,
                key.name,
                self.occupancy(key),
            )
        return {
            r.discord_id: {"token": r.token, "key_name": r.key.name, "provider": r.key.provider, "group": self.registry.group(r.key)}
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
            self.registry.release(r.token)
            self.reservations.pop(r.token, None)
        else:
            # A healthy stream changes owner only at a completed WAV boundary.
            self.registry.release(r.token)
            key = self.registry.reserve(r.token, self.order, "streaming")
            if key is None:
                self.reservations.pop(r.token, None)
            else:
                r.key, r.order = key, self.order

    def alternate(self, r: Reservation, attempted: set[str]) -> bool:
        self.registry.release(r.token)
        key = self.registry.reserve(r.token, r.order, "streaming", attempted)
        if key is None:
            return False
        r.key = key
        return True


def valid_configuration(settings, keys, order=None) -> bool:
    order = order or environment_order(settings)
    return any(
        getattr(k, "provider", "speechmatics") in order and (
            getattr(k, "provider", "speechmatics") == "deepgram" or
            (settings.speechmatics_realtime_model in {"enhanced", "standard"}
             and settings.speechmatics_realtime_language == "pt")) for k in keys)


async def open_provider(settings, pool, reservation):
    attempted: set[str] = set()
    deadline = time.monotonic() + 12
    while True:
        key = reservation.key
        attempted.add(key.name)
        upstream = None
        try:
            state = pool.registry.state(key, "streaming")
            if state != "healthy":
                raise ProviderError(state)
            if time.monotonic() >= deadline:
                raise ProviderError("recoverable")
            upstream = await connect(
                streaming_url(settings) if key.provider == "deepgram" else settings.speechmatics_realtime_url,
                additional_headers={"Authorization": ("Token " if key.provider == "deepgram" else "Bearer ") + key.value},
                open_timeout=min(4, max(0.1, deadline - time.monotonic())),
                close_timeout=2,
                ping_interval=15,
                ping_timeout=15,
                max_size=1 << 20,
                max_queue=16,
                write_limit=65536,
            )
            if key.provider == "deepgram":
                return upstream
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
                            **({"additional_vocab": [{"content": t} for t in settings.speechmatics_additional_vocab]} if settings.speechmatics_additional_vocab else {}),
                        },
                    }
                )
            )
            async with asyncio.timeout(min(4, max(0.1, deadline - time.monotonic()))):
                while True:
                    event = json.loads(await upstream.recv())
                    if event.get("message") == "RecognitionStarted":
                        return upstream
                    if event.get("message") == "Error":
                        raise message_error(event)
        except Exception as exc:
            if upstream is not None:
                await upstream.close()
            kind = exc.kind if isinstance(exc, ProviderError) else (deepgram_error(exc) if key.provider == "deepgram" else error_kind(exc))
            pool.registry.mark(key, "streaming", kind)
            logger.warning(
                "Realtime start failed user=%s key=%s reason=%s",
                reservation.discord_id,
                key.name,
                kind,
            )
            if kind not in {"invalid_input", "cancelled"} and time.monotonic() < deadline and pool.alternate(
                reservation, attempted
            ):
                continue
            if pool.registry.exhausted(reservation.order):
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
    unit_finished = False
    failure_reason = "recoverable"
    session_id = None
    attempt_id = None
    last_sent_at = time.monotonic()
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
                "transcription_provider": reservation.key.provider,
            },
            reservation.token,
            reservation.key.name,
        )
        model = settings.deepgram_model if reservation.key.provider == "deepgram" else settings.speechmatics_realtime_model
        await asyncio.to_thread(repository.set_recording_provider, recording_id, reservation.key.provider,
                                model, pool.registry.group(reservation.key))
        attempt_id = await asyncio.to_thread(repository.start_transcription_attempt, recording_id,
            "streaming", reservation.key.provider, reservation.key.name, pool.registry.group(reservation.key), model)
        await websocket.send_json({"type": "ready", "generation": generation, "recording_id": recording_id,
                                   "provider": reservation.key.provider})
        eos = asyncio.Event()

        async def send_audio():
            nonlocal frames, packets, sent_frames, last_sent_at
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
                        json.dumps({"type": "CloseStream"} if reservation.key.provider == "deepgram" else {"message": "EndOfStream", "last_seq_no": packets})
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
                last_sent_at = time.monotonic()
                if time.monotonic() - last_checkpoint >= 5:
                    last_checkpoint = time.monotonic()
                    await asyncio.to_thread(
                        repository.checkpoint_realtime_usage,
                        recording_id,
                        generation,
                        sent_frames / 48000,
                    )
                    await asyncio.to_thread(repository.checkpoint_transcription_attempt,
                        attempt_id, sent_frames / 48000)

        async def receive_results():
            close_metadata = False
            while True:
                try:
                    event = json.loads(await upstream.recv())
                except ConnectionClosedOK:
                    if reservation.key.provider == "deepgram" and eos.is_set() and close_metadata:
                        return
                    raise ProviderError("recoverable") from None
                if reservation.key.provider == "deepgram":
                    if event.get("type") == "Error" or event.get("err_code"):
                        raise deepgram_message_error(event)
                    if event.get("type") == "Metadata":
                        close_metadata = eos.is_set()
                        await asyncio.to_thread(repository.checkpoint_transcription_attempt, attempt_id,
                            sent_frames / 48000, remote_id=event.get("request_id"))
                    event = normalize_event(event)
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
                    words = event.get("words", final_words(event.get("results", [])))
                    for word in words:
                        validate_interval(word["start"], word["end"], frames / 48000)
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
                                    "words": words,
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

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                if reservation.key.provider == "deepgram" and not eos.is_set() and time.monotonic() - last_sent_at >= 5:
                    await upstream.send(json.dumps({"type": "KeepAlive"}))

        sender = asyncio.create_task(send_audio())
        receiver = asyncio.create_task(receive_results())
        watcher = asyncio.create_task(monitor())
        tasks = [sender, receiver, watcher, asyncio.create_task(heartbeat())]
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
        await asyncio.to_thread(RecordingCleanup(repository=repository, settings=settings).cleanup_completed_file, filename)
        await websocket.send_json({"type": "completed"})
    except Exception as exc:
        kind = (deepgram_error(exc) if reservation and reservation.key.provider == "deepgram"
                and not isinstance(exc, ProviderError) else error_kind(exc))
        if reservation is not None:
            pool.registry.mark(reservation.key, "streaming", kind)
            reservation.retiring = True
        if session_id is not None:
            session = await asyncio.to_thread(repository.get_voice_session, session_id)
            current_order, _ = await asyncio.to_thread(effective_order, settings, repository, session.guild_id if session else None)
            if pool.registry.exhausted(current_order) and not await asyncio.to_thread(repository.session_has_remote_jobs, session_id):
                await asyncio.to_thread(repository.discard_session_audio, session_id)
                kind = "no_credits"
        logger.warning(
            "Realtime fallback session=%s unit=%s reason=%s",
            session_id,
            recording_id,
            kind,
        )
        failure_reason = kind
        if recording_id is not None:
            await asyncio.to_thread(repository.set_recording_provider, recording_id, reservation.key.provider,
                model, pool.registry.group(reservation.key), kind)
            await asyncio.to_thread(repository.finish_realtime_unit, recording_id, generation, sent_frames / 48000, False)
            unit_finished = True
        try:
            await websocket.send_json({"type": "fallback", "reason": kind})
        except Exception:
            pass
    finally:
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if attempt_id is not None:
                    await asyncio.to_thread(repository.checkpoint_transcription_attempt, attempt_id,
                        sent_frames / 48000, "completed" if completed else "failed",
                        None if completed else failure_reason)
            finally:
                if recording_id is not None and not completed and not unit_finished:
                    await asyncio.to_thread(repository.finish_realtime_unit, recording_id,
                        generation, sent_frames / 48000, False)
        finally:
            try:
                if upstream is not None:
                    await upstream.close()
            finally:
                if reservation is not None:
                    if not completed:
                        reservation.retiring = True
                    pool.release_epoch(reservation)
                try:
                    await websocket.close()
                except Exception:
                    pass
