import io
import unittest
from unittest.mock import patch

from speech_synthesis import EXPRESSIONS, expressive_audio, speech_segments, synthesize


class SpeechExpressionTests(unittest.TestCase):
    def test_common_emoji_cues_follow_their_own_phrase(self):
        self.assertEqual(
            speech_segments("Boa ideia 😊. Isso tem piada 😂😂. Que pena 😢. Acabou 😭. A sério 😮? Estou zangado 😡."),
            [("Boa ideia.", "happy"), ("Isso tem piada.", "laugh"), ("Que pena.", "sad"),
             ("Acabou.", "cry"), ("A sério?", "surprised"), ("Estou zangado.", "angry")],
        )

    def test_prefix_cue_does_not_colour_the_rest_of_the_reply(self):
        self.assertEqual(speech_segments("😊 Olá! Segunda frase."), [("Olá!", "happy"), ("Segunda frase.", "neutral")])
        self.assertEqual(speech_segments("Primeira frase. Segunda frase 😊"), [("Primeira frase.", "neutral"), ("Segunda frase", "happy")])

    def test_laughter_wins_over_tears_when_both_are_in_a_joke(self):
        self.assertEqual(speech_segments("Muito bom 😂😭🤣"), [("Muito bom", "laugh")])
        self.assertEqual(speech_segments("Muito bom 😂 😂 😂"), [("Muito bom", "laugh")])

    def test_variants_modifiers_and_joined_unknown_emoji_are_silent(self):
        self.assertEqual(speech_segments("Obrigado ❤️🙏🏽. Boa 👍🏻. Programar 🧑‍💻🚀."),
                         [("Obrigado.", "warm"), ("Boa.", "happy"), ("Programar.", "neutral")])
        self.assertEqual(speech_segments("Olá ☺️"), [("Olá", "happy")])
        self.assertEqual(speech_segments("Olá ☹️"), [("Olá", "sad")])
        self.assertEqual(speech_segments("🚀🧑‍💻"), [])

    def test_emoji_only_reactions_and_neutral_numbers(self):
        self.assertEqual(speech_segments("😂😂"), [("", "laugh")])
        self.assertEqual(speech_segments("😭"), [("Oh...", "cry")])
        self.assertEqual(speech_segments("Tenho 3,5 euros. São 12:30!"), [("Tenho 3,5 euros. São 12:30!", "neutral")])

    def test_emoji_heavy_replies_keep_words_but_bound_tone_changes(self):
        source = " ".join(f"Frase {index} {'😊' if index % 2 else '😢'}" for index in range(30))
        segments = speech_segments(source)
        self.assertLessEqual(len(segments), 13)
        self.assertEqual(segments[-1][1], "neutral")
        for index in range(30):
            self.assertIn(f"Frase {index}", " ".join(text for text, _ in segments))

    def test_pitch_changes_preserve_duration_and_crying_modulates_voice(self):
        rate = 22050
        # An audible 200 Hz tone, one second of S16LE mono audio.
        import array
        import math

        samples = array.array("h", (int(8000 * math.sin(2 * math.pi * 200 * i / rate)) for i in range(rate)))
        if samples.itemsize != 2:
            self.skipTest("requires 16-bit samples")
        for mood in ("happy", "laugh", "cry"):
            with self.subTest(mood=mood):
                audio = expressive_audio(samples.tobytes(), EXPRESSIONS[mood], rate)
                self.assertGreater(len(audio), len(samples) * 2 * 0.9)
                self.assertLess(len(audio), len(samples) * 2 * 1.1)
                self.assertNotEqual(audio, samples.tobytes())
        self.assertEqual(expressive_audio(samples.tobytes(), EXPRESSIONS["neutral"], rate), samples.tobytes())

    def test_one_model_load_multiple_expressions_and_one_chuckle_per_group(self):
        class Chunk:
            audio_int16_bytes = b"\x01\x00" * 2205

        with patch("piper.PiperVoice.load") as load, patch("speech_synthesis.expressive_audio", side_effect=lambda audio, *_: audio):
            voice = load.return_value
            voice.config.sample_rate = 22050
            voice.synthesize.return_value = [Chunk()]
            output = io.BytesIO()
            synthesize("voice.onnx", "Boa 😂😂. Que pena 😢.", output)
            load.assert_called_once_with("voice.onnx")
            self.assertEqual(voice.synthesize.call_count, 2)
            first, second = voice.synthesize.call_args_list
            self.assertEqual(first.args[0], "Boa. Ah, ah!")
            self.assertEqual(first.args[1].length_scale, EXPRESSIONS["laugh"].length)
            self.assertEqual(second.args[0], "Que pena.")
            self.assertEqual(second.args[1].volume, EXPRESSIONS["sad"].volume)
            self.assertGreater(len(output.getvalue()), 2 * len(Chunk.audio_int16_bytes))

    def test_first_chunk_is_flushed_before_synthesizing_the_next(self):
        class Output(io.BytesIO):
            flushed = False

            def flush(self):
                self.flushed = True

        class Chunk:
            audio_int16_bytes = b"\x01\x00" * 2205

        output = Output()

        def chunks(*_):
            yield Chunk()
            self.assertTrue(output.flushed)
            self.assertEqual(output.getvalue(), Chunk.audio_int16_bytes)
            yield Chunk()

        with patch("piper.PiperVoice.load") as load:
            voice = load.return_value
            voice.config.sample_rate = 22050
            voice.synthesize.side_effect = chunks
            synthesize("voice.onnx", "Primeira frase. Segunda frase.", output)
        self.assertGreater(len(output.getvalue()), 2 * len(Chunk.audio_int16_bytes))


if __name__ == "__main__":
    unittest.main()
