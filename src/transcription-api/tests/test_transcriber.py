from __future__ import annotations

from pathlib import Path
from typing import Any

from app.transcriber import WhisperTranscriber


class FakeWhisperModel:
    def __init__(self) -> None:
        self.audio_path = ""
        self.options: dict[str, Any] = {}

    def transcribe(self, audio_path: str, **options: Any) -> dict[str, Any]:
        self.audio_path = audio_path
        self.options = options
        return {
            "text": " Ola mundo. ",
            "segments": [{"start": 1, "end": 2, "text": " Ola mundo. "}],
        }


def test_transcribe_passes_quality_options() -> None:
    model = FakeWhisperModel()
    transcriber = WhisperTranscriber(
        "turbo",
        language="pt",
        beam_size=5,
        initial_prompt="  FastAPI e Whisper  ",
        carry_initial_prompt=True,
        condition_on_previous_text=False,
    )
    transcriber._load_model = lambda: model  # type: ignore[method-assign]

    result = transcriber.transcribe(Path("call.wav"))

    assert result.text == "Ola mundo."
    assert result.segments[0].text == "Ola mundo."
    assert model.audio_path == "call.wav"
    assert model.options == {
        "beam_size": 5,
        "carry_initial_prompt": True,
        "condition_on_previous_text": False,
        "fp16": False,
        "initial_prompt": "FastAPI e Whisper",
        "language": "pt",
    }


def test_transcribe_can_disable_beam_search_and_detect_language() -> None:
    model = FakeWhisperModel()
    transcriber = WhisperTranscriber("turbo", language="auto", beam_size=0)
    transcriber._load_model = lambda: model  # type: ignore[method-assign]

    transcriber.transcribe(Path("mixed.wav"))

    assert model.options["beam_size"] is None
    assert model.options["language"] is None
    assert "initial_prompt" not in model.options
