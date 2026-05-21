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
    def __init__(self, model_name: str, device: str = "auto"):
        self.model_name = model_name
        self.device = device
        self._model: Any | None = None

    def transcribe(self, audio_path: Path) -> WhisperResult:
        model = self._load_model()
        result = model.transcribe(str(audio_path))
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
