import os
import sys
import threading
import time
import types
import wave
from pathlib import Path

from app.transcriber import WhisperTranscriber, configure_cpu_threads


class FakeModel:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def transcribe(self, audio_path: str, **options: object) -> dict[str, object]:
        self.options = options
        return {
            "text": "texto",
            "segments": [{"start": 1.0, "end": 2.0, "text": " texto "}],
        }


def test_configure_cpu_threads_sets_blas_env_vars(monkeypatch) -> None:
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    resolved = configure_cpu_threads(8)
    assert resolved == 8
    assert os.environ["OMP_NUM_THREADS"] == "8"
    assert os.environ["MKL_NUM_THREADS"] == "8"


def test_transcriber_uses_anti_hallucination_options() -> None:
    model = FakeModel()
    transcriber = WhisperTranscriber("turbo")
    transcriber._model = model

    result = transcriber.transcribe(Path("recording.wav"))

    assert result.text == "texto"
    assert result.segments[0].text == "texto"
    assert model.options["condition_on_previous_text"] is False
    assert model.options["compression_ratio_threshold"] == 2.0
    assert model.options["fp16"] is False
    assert model.options["hallucination_silence_threshold"] == 2.0
    assert model.options["logprob_threshold"] == -0.8
    assert model.options["no_speech_threshold"] == 0.6


def test_hallucination_silence_threshold_can_be_disabled() -> None:
    model = FakeModel()
    transcriber = WhisperTranscriber("turbo", hallucination_silence_threshold=0)
    transcriber._model = model

    transcriber.transcribe(Path("recording.wav"))

    assert "hallucination_silence_threshold" not in model.options


def test_segments_that_are_probably_unreliable_are_discarded() -> None:
    model = FakeModel()
    transcriber = WhisperTranscriber(
        "large-v3",
        max_no_speech_prob=0.6,
        logprob_threshold=-0.8,
        compression_ratio_threshold=2.0,
    )
    transcriber._model = model
    model.transcribe = lambda *_args, **_options: {
        "text": "Tchau. repetida frase valida baixa confianca",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Tchau.", "no_speech_prob": 0.95},
            {"start": 1.0, "end": 2.0, "text": "repetida", "compression_ratio": 2.5},
            {"start": 2.0, "end": 3.0, "text": "baixa confianca", "avg_logprob": -1.2},
            {
                "start": 3.0,
                "end": 4.0,
                "text": "frase valida",
                "no_speech_prob": 0.1,
                "avg_logprob": -0.2,
                "compression_ratio": 1.4,
            },
        ],
    }

    result = transcriber.transcribe(Path("recording.wav"))

    assert result.text == "frase valida"
    assert [segment.text for segment in result.segments] == ["frase valida"]


def test_fp16_is_used_only_after_loading_on_cuda() -> None:
    model = FakeModel()
    transcriber = WhisperTranscriber("large-v3", fp16=True)
    transcriber._model = model
    transcriber._loaded_device = "cuda"

    transcriber.transcribe(Path("recording.wav"))

    assert model.options["fp16"] is True


def test_vad_skips_silent_recordings_without_loading_model(monkeypatch, tmp_path) -> None:
    wav_path = tmp_path / "silence.wav"
    write_silence_wav(wav_path, seconds=1.0)

    class SilentVad:
        def __init__(self, _aggressiveness: int) -> None:
            pass

        def is_speech(self, _pcm: bytes, _sample_rate: int) -> bool:
            return False

    monkeypatch.setitem(sys.modules, "webrtcvad", types.SimpleNamespace(Vad=SilentVad))
    transcriber = WhisperTranscriber("turbo", vad_enabled=True)

    result = transcriber.transcribe(wav_path)

    assert result.text == ""
    assert result.segments == []
    assert transcriber._model is None


def test_vad_transcribes_only_speech_regions_with_original_offsets(monkeypatch, tmp_path) -> None:
    wav_path = tmp_path / "speech.wav"
    write_silence_wav(wav_path, seconds=2.0)

    class WindowVad:
        def __init__(self, _aggressiveness: int) -> None:
            self.calls = 0

        def is_speech(self, _pcm: bytes, _sample_rate: int) -> bool:
            self.calls += 1
            return 20 <= self.calls < 35

    monkeypatch.setitem(sys.modules, "webrtcvad", types.SimpleNamespace(Vad=WindowVad))
    model = FakeModel()
    model.transcribe = lambda *_args, **_options: {
        "segments": [{"start": 0.1, "end": 0.2, "text": " fala ", "no_speech_prob": 0.1}],
    }
    transcriber = WhisperTranscriber("turbo", vad_enabled=True)
    transcriber._model = model

    result = transcriber.transcribe(wav_path)

    assert result.text == "fala"
    assert len(result.segments) == 1
    assert round(result.segments[0].start, 2) == 0.17
    assert round(result.segments[0].end, 2) == 0.27


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


def write_silence_wav(path: Path, *, seconds: float, sample_rate: int = 16000) -> None:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
