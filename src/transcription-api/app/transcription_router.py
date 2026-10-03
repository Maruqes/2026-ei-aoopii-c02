from __future__ import annotations

import asyncio
import json
import uuid
import wave
from dataclasses import replace
from pathlib import Path

from .deepgram import DeepgramTranscriber, read_result, save_result
from .providers import effective_order, environment_order, registry_for
from .speechmatics_errors import NoCredits, ProviderError, error_kind
from .transcriber import SpeechmaticsTranscriber


class TranscriptionRouter:
    provider_name = "remote"
    model_name = "provider order"

    def __init__(self, settings):
        self.settings = settings
        self.registry = registry_for(settings)
        self.deepgram = DeepgramTranscriber(settings)

    def speechmatics(self, key):
        s = self.settings
        return SpeechmaticsTranscriber(
            "",
            api_keys=(key,),
            batch_url=s.speechmatics_batch_url,
            language=s.speechmatics_language,
            model=s.speechmatics_model,
            timeout_seconds=s.speechmatics_timeout_seconds,
            polling_interval_seconds=s.speechmatics_polling_interval_seconds,
            segment_gap_seconds=s.speechmatics_segment_gap_seconds,
            additional_vocab=s.speechmatics_additional_vocab,
        )

    def transcribe(self, path):
        return self.transcribe_recording(path)

    def transcribe_recording(
        self,
        path,
        *,
        repository=None,
        recording_id=None,
        session_id=None,
        job_id=None,
        key_name=None,
    ):
        path = Path(path)
        session = (
            repository.get_voice_session(session_id)
            if repository and session_id
            else None
        )
        order, _ = effective_order(
            self.settings, repository, session.guild_id if session else None
        )
        result_path = Path(str(path) + ".deepgram.json")
        if result_path.exists():
            return read_result(result_path)
        sidecar = Path(str(path) + ".speechmatics.json")
        original_model = "unknown"
        if sidecar.exists():
            remote = json.loads(sidecar.read_text(encoding="utf-8"))
            if not job_id:
                job_id, key_name = remote["job_id"], remote["key_name"]
            if remote["job_id"] == job_id:
                original_model = remote.get("model", "unknown")
        original = next(
            (
                k
                for k in self.registry.for_provider("speechmatics")
                if k.name == key_name
            ),
            None,
        )
        token = str(uuid.uuid4())
        attempted = set()
        active = (
            (lambda: repository.recording_is_live(recording_id))
            if recording_id
            else None
        )
        duration = 0.0
        if path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
            except (wave.Error, EOFError, ValueError):
                raise ProviderError("invalid_input") from None

        def save_job(remote_id, selected_name):
            nonlocal job_id
            job_id = remote_id
            if attempt_id:
                repository.checkpoint_transcription_attempt(
                    attempt_id, duration, remote_id=remote_id
                )
            temporary = Path(str(path) + ".speechmatics.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "job_id": remote_id,
                        "key_name": selected_name,
                        "provider": "speechmatics",
                        "model": model,
                    }
                ),
                encoding="utf-8",
            )
            temporary.replace(sidecar)
            if recording_id:
                repository.save_provider_job(recording_id, remote_id, selected_name)

        # Recover an accepted Speechmatics job with its original identity before changing provider.
        recover_original = bool(job_id)
        while True:
            if active and not active():
                raise ProviderError("cancelled")
            if recover_original:
                recover_original = False
                if original is None:
                    raise ProviderError("missing_job_key")
                key = original
                with self.registry.lock:
                    self.registry.leases[token] = (key, "batch")
            else:
                key = self.registry.reserve(token, order, "batch", attempted)
            if key is None:
                if self.registry.exhausted(order):
                    raise NoCredits()
                raise ProviderError(
                    "recoverable" if self.registry.usable(order) else "unconfigured"
                )
            attempted.add(key.name)
            attempt_id = None
            recovering_job = key.provider == "speechmatics" and bool(job_id)
            model = (
                self.settings.deepgram_model
                if key.provider == "deepgram"
                else self.settings.speechmatics_model
            )
            if recovering_job:
                model = original_model
            sent_seconds = 0.0

            def sent(seconds):
                nonlocal sent_seconds
                sent_seconds = max(sent_seconds, seconds)
                if attempt_id:
                    repository.checkpoint_transcription_attempt(
                        attempt_id, sent_seconds
                    )

            try:
                if recording_id:
                    existing = (
                        repository.existing_transcription_attempt(
                            recording_id, key.provider, job_id
                        )
                        if recovering_job
                        else None
                    )
                    if existing:
                        attempt_id, model = existing["id"], existing["model"]
                        repository.checkpoint_transcription_attempt(
                            attempt_id, 0, remote_id=job_id
                        )
                    else:
                        attempt_id = repository.start_transcription_attempt(
                            recording_id,
                            "batch",
                            key.provider,
                            key.name,
                            self.registry.group(key),
                            model,
                        )
                    repository.set_recording_provider(
                        recording_id, key.provider, model, self.registry.group(key)
                    )
                    if recovering_job:
                        repository.save_provider_job(recording_id, job_id, key.name)
                if key.provider == "deepgram":
                    result = self.deepgram.transcribe(
                        path, key, is_active=active, sent=sent
                    )
                else:
                    transcriber = self.speechmatics(key)
                    # Use the SDK's durable job flow even for uploads without a DB unit.
                    result = asyncio.run(
                        asyncio.wait_for(
                            transcriber._transcribe(
                                path,
                                key,
                                job_id=job_id,
                                save_job=save_job,
                                is_active=active,
                            ),
                            timeout=self.settings.speechmatics_timeout_seconds + 30,
                        )
                    )
                    if not recovering_job:
                        sent(duration)
                result = replace(
                    result,
                    provider=key.provider,
                    model=model,
                    key_name=key.name,
                    group=self.registry.group(key),
                )
                if key.provider == "deepgram":
                    save_result(result_path, result)
                if attempt_id:
                    repository.checkpoint_transcription_attempt(
                        attempt_id,
                        sent_seconds,
                        "completed",
                        remote_id=result.request_id or job_id,
                    )
                return result
            except Exception as exc:
                kind = error_kind(exc)
                self.registry.mark(key, "batch", kind)
                if attempt_id:
                    repository.checkpoint_transcription_attempt(
                        attempt_id, sent_seconds, "failed", kind, job_id
                    )
                if kind in {"invalid_input", "cancelled", "missing_job_key"}:
                    raise
                # Preserve a recoverable remote job through fallback failures/restarts.
                # A Deepgram request ID is stored only in its result, never in provider_job_id.
                if (
                    kind in {"no_credits", "invalid_key"}
                    and key.provider == "speechmatics"
                ):
                    sidecar.unlink(missing_ok=True)
                    if recording_id:
                        repository.save_provider_job(recording_id, None, None)
                job_id = None
                if not any(
                    k.name not in attempted and k.provider in order
                    for k in self.registry.keys
                ):
                    self.registry.release(token)
                    if self.registry.exhausted(order):
                        raise NoCredits() from None
                    raise ProviderError("recoverable") from None
            finally:
                self.registry.release(token)


def remote_mode(settings):
    return environment_order(settings) != ("whisper",)
