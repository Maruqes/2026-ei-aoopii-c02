from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    def __init__(self, model_name: str):
        self.model_name = model_name
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
            import whisper

            self._model = whisper.load_model(self.model_name)
        return self._model
