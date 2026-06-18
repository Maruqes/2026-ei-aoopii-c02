from __future__ import annotations

import logging
import os
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


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
class WhisperSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class WhisperResult:
    text: str
    segments: list[WhisperSegment]


@dataclass(frozen=True)
class SpeechRegion:
    start: float
    end: float


class WhisperTranscriber:
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")

    def __init__(
        self,
        model_name: str,
        device: str = "auto",
        *,
        language: str = "pt",
        beam_size: int = 5,
        initial_prompt: str = "",
        carry_initial_prompt: bool = False,
        condition_on_previous_text: bool = False,
        hallucination_silence_threshold: float = 2.0,
        max_no_speech_prob: float = 0.8,
        num_threads: int = 0,
        vad_enabled: bool = True,
        vad_aggressiveness: int = 3,
        vad_frame_ms: int = 30,
        vad_padding_ms: int = 300,
        vad_min_speech_ms: int = 250,
    ):
        self.model_name = model_name
        self.device = device
        self.num_threads = num_threads
        self.language = language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt
        self.carry_initial_prompt = carry_initial_prompt
        self.condition_on_previous_text = condition_on_previous_text
        self.hallucination_silence_threshold = hallucination_silence_threshold
        self.max_no_speech_prob = max_no_speech_prob
        self.vad_enabled = vad_enabled
        self.vad_aggressiveness = min(3, max(0, vad_aggressiveness))
        self.vad_frame_ms = vad_frame_ms if vad_frame_ms in {10, 20, 30} else 30
        self.vad_padding_ms = max(0, vad_padding_ms)
        self.vad_min_speech_ms = max(0, vad_min_speech_ms)
        self._model: Any | None = None

    def transcribe(self, audio_path: Path) -> WhisperResult:
        logger.info("whisper transcription queued file=%s", audio_path)
        future = self._executor.submit(self._transcribe, audio_path)
        return future.result()

    def _transcribe(self, audio_path: Path) -> WhisperResult:
        logger.info("whisper transcription started file=%s", audio_path)
        speech_regions = self._speech_regions(audio_path)
        if speech_regions == []:
            logger.info("vad found no speech file=%s", audio_path)
            return WhisperResult(text="", segments=[])

        model = self._load_model()
        if speech_regions is None:
            segments = self._transcribe_path(model, audio_path)
        else:
            segments = self._transcribe_speech_regions(model, audio_path, speech_regions)

        return WhisperResult(
            text=" ".join(segment.text for segment in segments),
            segments=segments,
        )

    def _transcribe_path(
        self,
        model: Any,
        audio_path: Path,
        *,
        offset_seconds: float = 0.0,
    ) -> list[WhisperSegment]:
        options: dict[str, Any] = {
            "beam_size": self.beam_size if self.beam_size > 0 else None,
            "condition_on_previous_text": self.condition_on_previous_text,
            "fp16": False,
            "language": self._language_option(),
            "temperature": 0.0,
        }
        if self.hallucination_silence_threshold > 0:
            options["hallucination_silence_threshold"] = self.hallucination_silence_threshold
        if self.initial_prompt.strip():
            options["initial_prompt"] = self.initial_prompt.strip()
            options["carry_initial_prompt"] = self.carry_initial_prompt

        result = model.transcribe(
            str(audio_path),
            **options,
        )
        return [
            WhisperSegment(
                start=float(segment.get("start", 0)) + offset_seconds,
                end=float(segment.get("end", 0)) + offset_seconds,
                text=str(segment.get("text", "")).strip(),
            )
            for segment in result.get("segments", [])
            if str(segment.get("text", "")).strip()
            and float(segment.get("no_speech_prob", 0.0)) < self.max_no_speech_prob
        ]

    def _transcribe_speech_regions(
        self,
        model: Any,
        audio_path: Path,
        speech_regions: list[SpeechRegion],
    ) -> list[WhisperSegment]:
        segments: list[WhisperSegment] = []
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
