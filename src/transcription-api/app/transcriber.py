from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .speechmatics_usage import (
    SpeechmaticsAPIKey,
    format_speechmatics_key_usage,
    select_speechmatics_api_key,
)

logger = logging.getLogger("uvicorn.error")

_ATTACH_TO_PREVIOUS = frozenset(",.;:!?%)]}»”’")
_ATTACH_TO_NEXT = frozenset("([{«“‘")
_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


@dataclass(frozen=True)
class TranscriptionSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    segments: list[TranscriptionSegment]


class Transcriber(Protocol):
    provider_name: str
    model_name: str

    def transcribe(self, audio_path: Path) -> TranscriptionResult: ...


def configure_cpu_threads(num_threads: int = 0) -> int:
    resolved = num_threads if num_threads > 0 else (os.cpu_count() or 1)
    for key in _THREAD_ENV_VARS:
        os.environ[key] = str(resolved)

    import torch

    torch.set_num_threads(resolved)
    torch.set_num_interop_threads(min(4, max(1, resolved // 4)))
    logger.info("whisper cpu threads configured threads=%d", resolved)
    return resolved


@dataclass(frozen=True)
class SpeechRegion:
    start: float
    end: float


WhisperSegment = TranscriptionSegment
WhisperResult = TranscriptionResult


class WhisperTranscriber:
    provider_name = "whisper"
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")

    def __init__(
        self,
        model_name: str,
        device: str = "auto",
        *,
        language: str = "pt",
        beam_size: int = 5,
        fp16: bool = True,
        initial_prompt: str = "",
        carry_initial_prompt: bool = False,
        condition_on_previous_text: bool = False,
        hallucination_silence_threshold: float = 2.0,
        max_no_speech_prob: float = 0.6,
        no_speech_threshold: float = 0.6,
        logprob_threshold: float = -0.8,
        compression_ratio_threshold: float = 2.0,
        num_threads: int = 0,
        vad_enabled: bool = True,
        vad_aggressiveness: int = 3,
        vad_frame_ms: int = 30,
        vad_padding_ms: int = 500,
        vad_min_speech_ms: int = 400,
    ):
        self.model_name = model_name
        self.device = device
        self.num_threads = num_threads
        self.language = language
        self.beam_size = beam_size
        self.fp16 = fp16
        self.initial_prompt = initial_prompt
        self.carry_initial_prompt = carry_initial_prompt
        self.condition_on_previous_text = condition_on_previous_text
        self.hallucination_silence_threshold = hallucination_silence_threshold
        self.max_no_speech_prob = max_no_speech_prob
        self.no_speech_threshold = no_speech_threshold
        self.logprob_threshold = logprob_threshold
        self.compression_ratio_threshold = compression_ratio_threshold
        self.vad_enabled = vad_enabled
        self.vad_aggressiveness = min(3, max(0, vad_aggressiveness))
        self.vad_frame_ms = vad_frame_ms if vad_frame_ms in {10, 20, 30} else 30
        self.vad_padding_ms = max(0, vad_padding_ms)
        self.vad_min_speech_ms = max(0, vad_min_speech_ms)
        self._model: Any | None = None
        self._loaded_device: str | None = None

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        logger.info("whisper transcription queued file=%s", audio_path)
        future = self._executor.submit(self._transcribe, audio_path)
        return future.result()

    def _transcribe(self, audio_path: Path) -> TranscriptionResult:
        logger.info("whisper transcription started file=%s", audio_path)
        speech_regions = self._speech_regions(audio_path)
        if speech_regions == []:
            logger.info("vad found no speech file=%s", audio_path)
            return TranscriptionResult(text="", segments=[])

        model = self._load_model()
        if speech_regions is None:
            segments = self._transcribe_path(model, audio_path)
        else:
            segments = self._transcribe_speech_regions(model, audio_path, speech_regions)

        return TranscriptionResult(
            text=" ".join(segment.text for segment in segments),
            segments=segments,
        )

    def _transcribe_path(
        self,
        model: Any,
        audio_path: Path,
        *,
        offset_seconds: float = 0.0,
    ) -> list[TranscriptionSegment]:
        options: dict[str, Any] = {
            "beam_size": self.beam_size if self.beam_size > 0 else None,
            "condition_on_previous_text": self.condition_on_previous_text,
            "compression_ratio_threshold": self.compression_ratio_threshold,
            "fp16": self._use_fp16(),
            "language": self._language_option(),
            "logprob_threshold": self.logprob_threshold,
            "no_speech_threshold": self.no_speech_threshold,
            "temperature": 0.0,
        }
        if self.hallucination_silence_threshold > 0:
            options["hallucination_silence_threshold"] = self.hallucination_silence_threshold
        if self.initial_prompt.strip():
            options["initial_prompt"] = self.initial_prompt.strip()
            options["carry_initial_prompt"] = self.carry_initial_prompt

        result = model.transcribe(str(audio_path), **options)
        return [
            TranscriptionSegment(
                start=float(segment.get("start", 0)) + offset_seconds,
                end=float(segment.get("end", 0)) + offset_seconds,
                text=str(segment.get("text", "")).strip(),
            )
            for segment in result.get("segments", [])
            if self._keep_segment(segment)
        ]

    def _keep_segment(self, segment: dict[str, Any]) -> bool:
        text = str(segment.get("text", "")).strip()
        if not text:
            return False
        if float(segment.get("no_speech_prob", 0.0)) >= self.max_no_speech_prob:
            return False
        if float(segment.get("avg_logprob", 0.0)) < self.logprob_threshold:
            return False
        if float(segment.get("compression_ratio", 0.0)) > self.compression_ratio_threshold:
            return False
        return True

    def _transcribe_speech_regions(
        self,
        model: Any,
        audio_path: Path,
        speech_regions: list[SpeechRegion],
    ) -> list[TranscriptionSegment]:
        segments: list[TranscriptionSegment] = []
        with tempfile.TemporaryDirectory(prefix="whisper-vad-") as tmp_dir:
            for index, region in enumerate(speech_regions):
                chunk_path = Path(tmp_dir) / f"speech-{index}.wav"
                actual_start = self._write_wav_region(audio_path, chunk_path, region)
                segments.extend(self._transcribe_path(model, chunk_path, offset_seconds=actual_start))
        return segments

    def _speech_regions(self, audio_path: Path) -> list[SpeechRegion] | None:
        if not self.vad_enabled or not audio_path.exists():
            return None

        try:
            import audioop
            import webrtcvad
        except ImportError:
            logger.warning("webrtcvad not installed; whisper will transcribe full audio file=%s", audio_path)
            return None

        try:
            with wave.open(str(audio_path), "rb") as wav:
                sample_rate = wav.getframerate()
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                if sample_rate not in {8000, 16000, 32000, 48000} or sample_width != 2:
                    logger.warning(
                        "vad unsupported wav format file=%s sample_rate=%d channels=%d sample_width=%d",
                        audio_path,
                        sample_rate,
                        channels,
                        sample_width,
                    )
                    return None

                vad = webrtcvad.Vad(self.vad_aggressiveness)
                frame_samples = sample_rate * self.vad_frame_ms // 1000
                frame_seconds = self.vad_frame_ms / 1000.0
                speech_frames: list[SpeechRegion] = []
                frame_index = 0

                while True:
                    pcm = wav.readframes(frame_samples)
                    if len(pcm) < frame_samples * channels * sample_width:
                        break
                    mono = audioop.tomono(pcm, sample_width, 0.5, 0.5) if channels > 1 else pcm
                    start = frame_index * frame_seconds
                    if vad.is_speech(mono, sample_rate):
                        speech_frames.append(SpeechRegion(start=start, end=start + frame_seconds))
                    frame_index += 1
        except (EOFError, wave.Error, OSError, ValueError) as exc:
            logger.warning("vad failed; whisper will transcribe full audio file=%s error=%s", audio_path, exc)
            return None

        return self._merge_speech_frames(speech_frames)

    def _merge_speech_frames(self, speech_frames: list[SpeechRegion]) -> list[SpeechRegion]:
        if not speech_frames:
            return []

        max_gap = self.vad_padding_ms / 1000.0
        min_duration = self.vad_min_speech_ms / 1000.0
        merged: list[SpeechRegion] = []
        current = speech_frames[0]

        for frame in speech_frames[1:]:
            if frame.start - current.end <= max_gap:
                current = SpeechRegion(start=current.start, end=frame.end)
                continue
            if current.end - current.start >= min_duration:
                merged.append(current)
            current = frame

        if current.end - current.start >= min_duration:
            merged.append(current)

        padding = self.vad_padding_ms / 1000.0
        expanded: list[SpeechRegion] = []
        for region in merged:
            start = max(0.0, region.start - padding)
            end = region.end + padding
            if expanded and start <= expanded[-1].end:
                expanded[-1] = SpeechRegion(start=expanded[-1].start, end=max(expanded[-1].end, end))
            else:
                expanded.append(SpeechRegion(start=start, end=end))
        return expanded

    def _write_wav_region(self, source_path: Path, destination_path: Path, region: SpeechRegion) -> float:
        with wave.open(str(source_path), "rb") as source:
            sample_rate = source.getframerate()
            start_frame = max(0, int(region.start * sample_rate))
            end_frame = max(start_frame, int(region.end * sample_rate))
            source.setpos(min(start_frame, source.getnframes()))
            pcm = source.readframes(max(0, end_frame - start_frame))

            with wave.open(str(destination_path), "wb") as destination:
                destination.setnchannels(source.getnchannels())
                destination.setsampwidth(source.getsampwidth())
                destination.setframerate(sample_rate)
                destination.writeframes(pcm)

        return start_frame / sample_rate

    def _load_model(self) -> Any:
        if self._model is None:
            configure_cpu_threads(self.num_threads)
            import torch
            import whisper

            device = self._resolve_device(torch)
            logger.info("loading whisper model=%s device=%s", self.model_name, device)
            self._model = whisper.load_model(self.model_name, device=device)
            self._loaded_device = device
        return self._model

    def _resolve_device(self, torch: Any) -> str:
        if self.device == "auto":
            return "cuda" if torch.cuda.is_available() else "cpu"
        if self.device not in {"cuda", "cpu"}:
            raise RuntimeError(f"Unsupported WHISPER_DEVICE: {self.device}. Use 'auto', 'cuda', or 'cpu'.")
        if self.device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("WHISPER_DEVICE=cuda was requested, but CUDA is not available in this container.")
        return self.device

    def _language_option(self) -> str | None:
        language = self.language.strip()
        if language.lower() in {"", "auto", "detect"}:
            return None
        return language

    def _use_fp16(self) -> bool:
        return self.fp16 and self._loaded_device == "cuda"


class SpeechmaticsTranscriber:
    provider_name = "speechmatics"

    def __init__(
        self,
        api_key: str,
        *,
        api_keys: tuple[SpeechmaticsAPIKey, ...] = (),
        batch_url: str = "https://eu1.asr.api.speechmatics.com/v2",
        language: str = "multi",
        model: str = "melia-1",
        usage_limit_hours: float = 50.0,
        polling_interval_seconds: float = 2.0,
        timeout_seconds: float = 600.0,
        segment_gap_seconds: float = 1.5,
        additional_vocab: tuple[str, ...] = (),
        client_factory: Callable[..., Any] | None = None,
    ):
        self.api_keys = api_keys or _speechmatics_api_keys_from_single(api_key)
        self.batch_url = batch_url.rstrip("/")
        self.language = language.strip() or "multi"
        self.model_name = model.strip().lower() or "melia-1"
        self.usage_limit_hours = max(0.0, usage_limit_hours)
        self.polling_interval_seconds = max(0.1, polling_interval_seconds)
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.segment_gap_seconds = max(0.0, segment_gap_seconds)
        self.additional_vocab = tuple(term.strip() for term in additional_vocab if term.strip())
        self._client_factory = client_factory

        if not self.api_keys:
            raise RuntimeError("SPEECHMATICS_API_KEY is required")
        if self.model_name not in {"standard", "enhanced", "melia-1"}:
            raise RuntimeError(
                "Unsupported SPEECHMATICS_MODEL. Use 'standard', 'enhanced', or 'melia-1'."
            )
        if self.model_name == "melia-1" and self.language != "multi":
            raise RuntimeError("SPEECHMATICS_LANGUAGE must be 'multi' when SPEECHMATICS_MODEL=melia-1")
        if self.model_name == "melia-1" and self.additional_vocab:
            raise RuntimeError("SPEECHMATICS_ADDITIONAL_VOCAB is not supported by SPEECHMATICS_MODEL=melia-1")

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        logger.info(
            "speechmatics transcription started file=%s model=%s language=%s",
            audio_path,
            self.model_name,
            self.language,
        )
        selected_key = self._select_api_key()
        return asyncio.run(self._transcribe(audio_path, selected_key.value))

    def _select_api_key(self) -> SpeechmaticsAPIKey:
        if len(self.api_keys) == 1:
            return self.api_keys[0]

        selected = select_speechmatics_api_key(
            api_keys=self.api_keys,
            batch_url=self.batch_url,
            limit_hours=self.usage_limit_hours,
            timeout_seconds=10.0,
        )
        logger.info("speechmatics api key selected %s", format_speechmatics_key_usage(selected))
        return selected.key

    async def _transcribe(self, audio_path: Path, api_key: str) -> TranscriptionResult:
        from speechmatics.batch import AsyncClient, Transcript, TranscriptionConfig

        client_factory = self._client_factory or AsyncClient
        config = TranscriptionConfig(
            language=self.language,
            model=self.model_name,
            additional_vocab=[{"content": term} for term in self.additional_vocab] or None,
        )

        async with client_factory(api_key=api_key, url=self.batch_url) as client:
            transcript = await client.transcribe(
                str(audio_path),
                transcription_config=config,
                polling_interval=self.polling_interval_seconds,
                timeout=self.timeout_seconds,
            )

        if not isinstance(transcript, Transcript):
            raise RuntimeError("Speechmatics returned an unexpected non-JSON transcript")

        segments = self._segments_from_results(transcript.results)
        return TranscriptionResult(
            text=" ".join(segment.text for segment in segments),
            segments=segments,
        )

    def _segments_from_results(self, results: list[Any]) -> list[TranscriptionSegment]:
        segments: list[TranscriptionSegment] = []
        text = ""
        start: float | None = None
        end: float | None = None

        def flush() -> None:
            nonlocal text, start, end
            normalized = text.strip()
            if normalized and start is not None and end is not None:
                segments.append(TranscriptionSegment(start=start, end=end, text=normalized))
            text = ""
            start = None
            end = None

        for item in results:
            alternatives = getattr(item, "alternatives", None) or []
            if not alternatives:
                continue

            content = str(getattr(alternatives[0], "content", "")).strip()
            if not content:
                continue

            item_type = str(getattr(item, "type", ""))
            item_start = float(getattr(item, "start_time", 0.0))
            item_end = float(getattr(item, "end_time", item_start))

            if (
                item_type == "word"
                and text
                and end is not None
                and self.segment_gap_seconds > 0
                and item_start - end >= self.segment_gap_seconds
            ):
                flush()

            if start is None and item_type == "word":
                start = item_start
            if start is None:
                start = item_start

            text = _append_token(
                text,
                content,
                attaches_to=getattr(item, "attaches_to", None),
            )
            end = max(end or item_end, item_end)

            if bool(getattr(item, "is_eos", False)):
                flush()

        flush()
        return segments


def _append_token(text: str, token: str, *, attaches_to: str | None) -> str:
    if not text:
        return token
    if attaches_to == "previous" or token[0] in _ATTACH_TO_PREVIOUS:
        return text + token
    if text[-1] in _ATTACH_TO_NEXT:
        return text + token
    return f"{text} {token}"


def _speechmatics_api_keys_from_single(api_key: str) -> tuple[SpeechmaticsAPIKey, ...]:
    api_key = api_key.strip()
    if not api_key:
        return ()
    return (SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY", value=api_key),)
