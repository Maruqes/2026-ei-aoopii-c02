"""Deepgram wire protocol; no SDK and no credentials in durable artifacts."""

from __future__ import annotations

import asyncio
import json
import math
import os
import wave
from array import array
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .speechmatics_errors import ProviderError
from .transcriber import TranscriptionResult, TranscriptionSegment


def params(settings, *, streaming=False):
    values = [
        ("model", settings.deepgram_model),
        ("language", settings.deepgram_language),
        ("punctuate", "true"),
    ]
    values.extend(("keyterm", term) for term in settings.deepgram_keyterms)
    if streaming:
        values.extend(
            (
                ("encoding", "linear16"),
                ("sample_rate", "48000"),
                ("channels", "1"),
                ("interim_results", "true"),
                ("endpointing", str(settings.deepgram_endpointing_ms)),
            )
        )
    else:
        values.append(("utterances", "true"))
    return values


def streaming_url(settings):
    return (
        settings.deepgram_realtime_url
        + "?"
        + urlencode(params(settings, streaming=True))
    )


def deepgram_message_error(event):
    return ProviderError(
        {
            "INVALID_AUTH": "invalid_key",
            "INSUFFICIENT_PERMISSIONS": "invalid_key",
            "ASR_PAYMENT_REQUIRED": "no_credits",
            "TOO_MANY_REQUESTS": "capacity",
            "INVALID_QUERY_PARAMETER": "invalid_input",
        }.get(event.get("err_code") or event.get("code"), "recoverable")
    )


def deepgram_error(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status", None)
    return {
        401: "invalid_key",
        403: "invalid_key",
        402: "no_credits",
        429: "capacity",
        400: "invalid_input",
        422: "invalid_input",
    }.get(status, "recoverable")


def validate_interval(start, end, duration):
    start, end = float(start), float(end)
    if (
        not math.isfinite(start)
        or not math.isfinite(end)
        or start < 0
        or end < start
        or end > duration + 0.1
    ):
        raise ProviderError("invalid_input")
    return start, end


def normalize_event(event):
    if event.get("type") != "Results":
        return event
    alternative = event["channel"]["alternatives"][0]
    words = [
        {
            "text": w.get("punctuated_word") or w["word"],
            "start": w["start"],
            "end": w["end"],
        }
        for w in alternative.get("words", [])
    ]
    return {
        "message": "AddTranscript" if event.get("is_final") else "AddPartialTranscript",
        "metadata": {
            "transcript": alternative["transcript"],
            "start_time": event["start"],
            "end_time": event["start"] + event["duration"],
        },
        "words": words,
    }


def read_result(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["segments"] = [TranscriptionSegment(**s) for s in payload["segments"]]
    if payload.get("provider_completed_at"):
        payload["provider_completed_at"] = datetime.fromisoformat(
            payload["provider_completed_at"]
        )
    return TranscriptionResult(**payload)


def save_result(path, result):
    payload = asdict(result)
    if result.provider_completed_at:
        payload["provider_completed_at"] = result.provider_completed_at.isoformat()
    temporary = Path(str(path) + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(payload, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


class DeepgramTranscriber:
    provider_name = "deepgram"

    def __init__(self, settings, *, client_factory=httpx.AsyncClient):
        self.settings = settings
        self.model_name = settings.deepgram_model
        self.client_factory = client_factory

    def transcribe(self, audio_path, key, *, is_active=None, sent=None):
        derived = Path(str(audio_path) + ".deepgram-mono.wav")
        try:
            if derived.is_symlink():
                raise ProviderError("invalid_input")
            try:
                with wave.open(str(audio_path), "rb") as source:
                    channels, rate = source.getnchannels(), source.getframerate()
                    count = source.getnframes()
                    if (
                        source.getsampwidth() != 2
                        or channels not in {1, 2}
                        or rate <= 0
                    ):
                        raise ProviderError("invalid_input")
                    duration = count / rate
                    with wave.open(str(derived), "wb") as target:
                        target.setnchannels(1)
                        target.setsampwidth(2)
                        target.setframerate(rate)
                        copied = 0
                        while data := source.readframes(rate):
                            if is_active and not is_active():
                                raise ProviderError("cancelled")
                            samples = array("h", data)
                            if channels == 2:
                                samples = array(
                                    "h",
                                    (
                                        int((samples[i] + samples[i + 1]) / 2)
                                        for i in range(0, len(samples), 2)
                                    ),
                                )
                            target.writeframes(samples.tobytes())
                            copied += len(samples)
                        if copied != count:
                            raise ProviderError("invalid_input")
            except (wave.Error, EOFError, ValueError) as exc:
                raise ProviderError("invalid_input") from exc

            uploaded_bytes = 0

            async def chunks():
                nonlocal uploaded_bytes
                with derived.open("rb") as audio:
                    while chunk := audio.read(65536):
                        if is_active and not await asyncio.to_thread(is_active):
                            raise ProviderError("cancelled")
                        yield chunk
                        uploaded_bytes += len(chunk)

            async def recognize():
                async with self.client_factory(
                    timeout=self.settings.speechmatics_timeout_seconds
                ) as client:
                    response = await client.post(
                        self.settings.deepgram_api_base_url.rstrip("/") + "/listen",
                        params=params(self.settings),
                        headers={
                            "Authorization": "Token " + key.value,
                            "Content-Type": "audio/wav",
                        },
                        content=chunks(),
                    )
                    response.raise_for_status()
                    return response.json()

            async def request():
                task = asyncio.create_task(recognize())
                try:
                    while not task.done():
                        if is_active and not await asyncio.to_thread(is_active):
                            raise ProviderError("cancelled")
                        await asyncio.wait({task}, timeout=0.5)
                    return await task
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            try:
                payload = asyncio.run(
                    asyncio.wait_for(
                        request(), timeout=self.settings.speechmatics_timeout_seconds
                    )
                )
            except httpx.HTTPError as exc:
                raise ProviderError(deepgram_error(exc)) from None
            finally:
                if sent:
                    sent(min(duration, max(0, uploaded_bytes - 44) / (2 * rate)))
            if is_active and not is_active():
                raise ProviderError("cancelled")
            result = self.normalize(payload, duration)
            return TranscriptionResult(
                result.text,
                result.segments,
                datetime.now(timezone.utc),
                duration,
                "deepgram",
                self.model_name,
                key.name,
                key.group,
                payload.get("metadata", {}).get("request_id"),
            )
        finally:
            derived.unlink(missing_ok=True)

    def normalize(self, payload, duration):
        results = payload["results"]
        alternative = results["channels"][0]["alternatives"][0]
        segments = []
        items = results.get("utterances") or alternative.get("words", [])
        for item in items:
            start, end = validate_interval(item["start"], item["end"], duration)
            text = (
                item.get("transcript")
                or item.get("punctuated_word")
                or item.get("word")
                or ""
            ).strip()
            if text:
                segments.append(TranscriptionSegment(start, end, text))
        transcript = alternative.get("transcript", "").strip()
        if transcript and not segments:
            segments = [TranscriptionSegment(0, duration, transcript)]
        return TranscriptionResult(transcript, segments, duration_seconds=duration)
