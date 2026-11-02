"""Portuguese speech for Discord: neural Edge voice with a local Piper fallback.

Emoji cues shape prosody without being read aloud. Edge TTS applies native
rate/pitch/volume changes; Piper is only a last resort when Edge is unreachable.
"""

import asyncio
import os
import re
import subprocess
import sys
from typing import NamedTuple

SAMPLE_RATE = 22050  # Contract with the Go bot and its ffmpeg/Opus pipeline.
DEFAULT_EDGE_VOICE = "pt-PT-RaquelNeural"
EDGE_CONCURRENCY = 4


class Expression(NamedTuple):
    rate: int  # Edge relative rate, percent.
    pitch: int  # Edge relative pitch, Hz.
    volume: int  # Edge relative volume, percent.
    pause: float  # Silence after the segment, seconds.
    length: float = 1.05  # Piper length_scale (1.0 is the model default).
    tremolo: float = 0.0  # Piper-only, very restrained.
    reaction: str = ""


# Restrained values: the voice should stay natural, not theatrical.
EXPRESSIONS = {
    "neutral": Expression(0, 0, 0, 0.28, 1.05),
    "happy": Expression(6, 8, 0, 0.28, 1.0),
    "warm": Expression(-4, -2, -4, 0.32, 1.1),
    "playful": Expression(4, 6, 0, 0.28, 1.03),
    "thoughtful": Expression(-8, -4, -3, 0.4, 1.12),
    "sleepy": Expression(-12, -8, -8, 0.45, 1.18),
    "nervous": Expression(5, 4, -2, 0.28, 1.06, tremolo=0.04),
    "surprised": Expression(8, 12, 0, 0.3, 1.0),
    "sad": Expression(-12, -7, -8, 0.42, 1.15),
    "angry": Expression(4, -3, 3, 0.25, 1.0),
    "cry": Expression(-14, -9, -9, 0.45, 1.2, tremolo=0.08),
    "laugh": Expression(5, 8, 0, 0.22, 1.0, reaction="Ah, ah!"),
}
EMOJI_GROUPS = {
    "happy": "😀😃😄😁😊🙂☺🥳🎉🎊👏✨🔥💪👍✅",
    "warm": "🥰😍😘😚😙😗🤗🥹❤💕💖💗💙💚💛🧡💜🤍🖤🙏",
    "playful": "😉😜😝😛😎😏🙃🤭🙄😒",
    "thoughtful": "🤔🧐",
    "sleepy": "😴🥱😪",
    "nervous": "😅😬🫣😳🫠",
    "surprised": "😮😯😲😱🤯😨😰",
    "sad": "😢😔😞😟🙁☹🥺💔",
    "angry": "😡😠🤬😤",
    "cry": "😭",
    "laugh": "😂🤣😆",
}
EMOJI_MOODS = {emoji: mood for mood, emojis in EMOJI_GROUPS.items() for emoji in emojis}
PRIORITY = {mood: index for index, mood in enumerate(EXPRESSIONS)}
# Consume modifiers, variation selectors and joined emoji as one cue. Unknown emoji stay silent.
EMOJI_RUN = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0e\ufe0f\u200d\u20e3]+")
SENTENCES = re.compile(r"(?<=[.!?;])\s+(?=\w)|\n+")
SPACES = re.compile(r"\s+")


def speech_segments(text):
    segments = []
    pending = "neutral"
    cursor = 0

    def add_text(fragment):
        nonlocal pending
        # Emoji already became a mood; never let the synthesizer announce them.
        fragment = EMOJI_RUN.sub(" ", fragment)
        punctuation = re.match(r"^\s*([.!?,;:]+)", fragment)
        if punctuation and segments:
            phrase, mood = segments[-1]
            segments[-1] = (phrase + punctuation[1], mood)
            fragment = fragment[punctuation.end():]
        phrases = [p.strip() for p in SENTENCES.split(fragment) if any(c.isalnum() for c in p)]
        for phrase in phrases:
            segments.append((phrase, pending))
            pending = "neutral"
        return bool(phrases)

    for match in EMOJI_RUN.finditer(text):
        phrases = add_text(text[cursor:match.start()])
        moods = [EMOJI_MOODS[c] for c in match.group() if c in EMOJI_MOODS]
        mood = max(moods, key=PRIORITY.get, default="neutral")
        if segments and mood != "neutral":
            phrase, previous = segments[-1]
            segments[-1] = (phrase, mood if phrases else max((previous, mood), key=PRIORITY.get))
        elif not segments:
            pending = max((pending, mood), key=PRIORITY.get)
        cursor = match.end()
    add_text(text[cursor:])
    if not segments and pending in {"laugh", "cry", "surprised"}:
        reaction = {"laugh": "", "cry": "Oh...", "surprised": "Oh!"}[pending]
        segments.append((reaction, pending))

    # ponytail: cap tone changes at 12; keep remaining words neutral in emoji-heavy replies.
    merged = []
    for phrase, mood in segments:
        if len(merged) >= 12:
            mood = "neutral"
        if merged and merged[-1][1] == mood:
            merged[-1] = (merged[-1][0] + " " + phrase, mood)
        else:
            merged.append((phrase, mood))
    return merged


def prepare_segments(text):
    prepared = []
    for phrase, mood in speech_segments(text):
        expression = EXPRESSIONS[mood]
        phrase = SPACES.sub(" ", phrase).strip()
        if expression.reaction:
            phrase = phrase.rstrip(" ,;:")
            phrase += (". " if phrase and phrase[-1] not in ".!?" else " ") + expression.reaction
        if phrase:
            prepared.append((phrase, expression))
    return prepared


def silence(seconds):
    return bytes(round(SAMPLE_RATE * seconds) * 2)


def expressive_audio(audio, expression, sample_rate):
    if not expression.tremolo:
        return audio
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
         "-af", f"tremolo=f=6:d={expression.tremolo}", "-f", "s16le", "pipe:1"],
        input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError("ffmpeg could not apply expression: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


class PiperSynthesizer:
    """Load Piper once per reply and reuse it for every fallback segment."""

    def __init__(self, model):
        if not model or not os.path.exists(model):
            raise FileNotFoundError("Piper voice model not found: " + (model or "(unset)"))
        self.model = model
        self.voice = None
        self.rate = SAMPLE_RATE

    def load(self):
        if self.voice is None:
            from piper import PiperVoice

            self.voice = PiperVoice.load(self.model)
            self.rate = self.voice.config.sample_rate
            if self.rate != SAMPLE_RATE:
                raise ValueError(f"The Discord speech model must produce {SAMPLE_RATE} Hz audio")

    def chunks(self, text, expression):
        from piper import SynthesisConfig

        self.load()
        config = SynthesisConfig(length_scale=expression.length, volume=1.0)
        for chunk in self.voice.synthesize(text, config):
            if chunk.audio_int16_bytes:
                yield expressive_audio(chunk.audio_int16_bytes, expression, self.rate)


def write_chunks(chunks, output, previous_pause):
    wrote = False
    for chunk in chunks:
        if not wrote and previous_pause:
            output.write(silence(previous_pause))
        wrote = True
        output.write(chunk)
        output.flush()  # Let Discord start speaking before the whole reply is ready.
    return wrote


def decode_mp3(audio):
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError("ffmpeg could not decode Edge audio: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


async def edge_pcm(text, voice, expression, semaphore):
    import edge_tts

    async with semaphore:
        communicate = edge_tts.Communicate(
            text,
            voice,
            rate=f"{expression.rate:+d}%",
            pitch=f"{expression.pitch:+d}Hz",
            volume=f"{expression.volume:+d}%",
        )
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
    if not audio:
        raise RuntimeError("Edge TTS returned no audio")
    return await asyncio.to_thread(decode_mp3, bytes(audio))


def synthesize_piper(segments, model, output):
    piper = PiperSynthesizer(model)
    previous_pause = 0.0
    for text, expression in segments:
        if write_chunks(piper.chunks(text, expression), output, previous_pause):
            previous_pause = expression.pause


async def synthesize_edge(segments, voice, model, output):
    semaphore = asyncio.Semaphore(EDGE_CONCURRENCY)
    tasks = [asyncio.create_task(edge_pcm(text, voice, expression, semaphore)) for text, expression in segments]
    piper = None
    edge_failed = False
    previous_pause = 0.0
    try:
        for index, (text, expression) in enumerate(segments):
            pcm = None
            if not edge_failed:
                try:
                    pcm = await tasks[index]
                except Exception as exc:
                    edge_failed = True
                    print(f"Edge TTS failed; using Piper for the rest of this reply: {exc}", file=sys.stderr)
                    for pending in tasks[index + 1:]:
                        pending.cancel()
            if pcm is not None:
                if write_chunks([pcm], output, previous_pause):
                    previous_pause = expression.pause
                continue
            if piper is None:
                piper = PiperSynthesizer(model)
            if write_chunks(await asyncio.to_thread(lambda t=text, e=expression: list(piper.chunks(t, e))), output, previous_pause):
                previous_pause = expression.pause
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def synthesize(provider, voice, model, text, output):
    segments = prepare_segments(text)
    if not segments:
        return
    if provider == "piper":
        synthesize_piper(segments, model, output)
        return
    asyncio.run(synthesize_edge(segments, voice, model, output))


if __name__ == "__main__":
    provider = sys.argv[1] if len(sys.argv) > 1 else "edge"
    voice = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_EDGE_VOICE
    model = sys.argv[3] if len(sys.argv) > 3 else ""
    try:
        synthesize(provider, voice, model, sys.stdin.read(), sys.stdout.buffer)
    except Exception as exc:
        print(f"speech synthesis failed: {exc}", file=sys.stderr)
        sys.exit(1)
