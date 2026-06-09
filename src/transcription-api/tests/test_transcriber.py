from pathlib import Path

from app.transcriber import WhisperTranscriber


class FakeModel:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def transcribe(self, audio_path: str, **options: object) -> dict[str, object]:
        self.options = options
        return {
            "text": "texto",
            "segments": [{"start": 1.0, "end": 2.0, "text": " texto "}],
        }


def test_transcriber_uses_anti_hallucination_options() -> None:
    model = FakeModel()
    transcriber = WhisperTranscriber("turbo")
    transcriber._model = model

    result = transcriber.transcribe(Path("recording.wav"))

    assert result.text == "texto"
    assert result.segments[0].text == "texto"
    assert model.options["condition_on_previous_text"] is False
    assert model.options["hallucination_silence_threshold"] == 2.0


def test_hallucination_silence_threshold_can_be_disabled() -> None:
    model = FakeModel()
    transcriber = WhisperTranscriber("turbo", hallucination_silence_threshold=0)
    transcriber._model = model

    transcriber.transcribe(Path("recording.wav"))

    assert "hallucination_silence_threshold" not in model.options


def test_segments_that_are_probably_silence_are_discarded() -> None:
    model = FakeModel()
    transcriber = WhisperTranscriber("large-v3", max_no_speech_prob=0.8)
    transcriber._model = model
    model.transcribe = lambda *_args, **_options: {
        "text": "Tchau. frase valida",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Tchau.", "no_speech_prob": 0.95},
            {"start": 1.0, "end": 2.0, "text": "frase valida", "no_speech_prob": 0.1},
        ],
    }

    result = transcriber.transcribe(Path("recording.wav"))

    assert result.text == "frase valida"
    assert [segment.text for segment in result.segments] == ["frase valida"]
