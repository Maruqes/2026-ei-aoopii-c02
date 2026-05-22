from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class WhisperSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class WhisperResult:
    text: str
    segments: list[WhisperSegment]


class WhisperTranscriber:
    def __init__(
        self,
        model_name: str,
        device: str = "auto",
        *,
        language: str = "pt",
        beam_size: int = 5,
        initial_prompt: str = "",
        carry_initial_prompt: bool = False,
        condition_on_previous_text: bool = True,
    ):
        self.model_name = model_name
        self.device = device
        self.language = language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt
        self.carry_initial_prompt = carry_initial_prompt
        self.condition_on_previous_text = condition_on_previous_text
        self._model: Any | None = None

    def transcribe(self, audio_path: Path) -> WhisperResult:
        model = self._load_model()
        options: dict[str, Any] = {
            "beam_size": self.beam_size if self.beam_size > 0 else None,
            "condition_on_previous_text": self.condition_on_previous_text,
            "fp16": False,
            "language": self._language_option(),
        }
        if self.initial_prompt.strip():
            options["initial_prompt"] = self.initial_prompt.strip()
            options["carry_initial_prompt"] = self.carry_initial_prompt

        result = model.transcribe(
            str(audio_path),
            **options,
        )
        segments = [
            WhisperSegment(
                start=float(segment.get("start", 0)),
                end=float(segment.get("end", 0)),
                text=str(segment.get("text", "")).strip(),
            )
            for segment in result.get("segments", [])
            if str(segment.get("text", "")).strip()
        ]
        return WhisperResult(
            text=str(result.get("text", "")).strip(),
            segments=segments,
        )

    def _load_model(self) -> Any:
        if self._model is None:
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
