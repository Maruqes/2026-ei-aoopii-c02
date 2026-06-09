import threading
import time
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


def test_whisper_transcriptions_run_one_at_a_time() -> None:
    state_lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()
    active_calls = 0
    max_active_calls = 0
    call_order: list[str] = []

    class BlockingModel:
        def transcribe(self, audio_path: str, **_options: object) -> dict[str, object]:
            nonlocal active_calls, max_active_calls
            with state_lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
                call_order.append(audio_path)

            if audio_path == "first.wav":
                first_started.set()
                assert release_first.wait(timeout=2)

            with state_lock:
                active_calls -= 1
            return {"segments": [{"start": 0, "end": 1, "text": audio_path}]}

    transcriber = WhisperTranscriber("turbo")
    transcriber._model = BlockingModel()
    results: dict[str, str] = {}

    first = threading.Thread(
        target=lambda: results.update(first=transcriber.transcribe(Path("first.wav")).text)
    )
    second = threading.Thread(
        target=lambda: results.update(second=transcriber.transcribe(Path("second.wav")).text)
    )

    first.start()
    assert first_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)

    assert max_active_calls == 1
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert max_active_calls == 1
    assert call_order == ["first.wav", "second.wav"]
    assert results == {"first": "first.wav", "second": "second.wav"}
