"""Local Piper speech: emoji cues shape prosody without being read aloud."""

import re
import subprocess
import sys
from typing import NamedTuple


class Expression(NamedTuple):
    length: float
    pitch: float
    volume: float
    pause: float
    reaction: str = ""
    tremolo: float = 0.0


# Tugão has no native emotion control: these are restrained prosody approximations.
EXPRESSIONS = {
    "neutral": Expression(1.3, 1.0, 1.0, 0.4),
    "happy": Expression(1.25, 1.04, 0.95, 0.4),
    "warm": Expression(1.4, 1.02, 0.85, 0.45),
    "playful": Expression(1.35, 1.02, 0.9, 0.45),
    "thoughtful": Expression(1.45, 1.0, 0.9, 0.55),
    "sleepy": Expression(1.55, 0.97, 0.8, 0.6),
    "nervous": Expression(1.4, 1.02, 0.85, 0.45, tremolo=0.08),
    "surprised": Expression(1.3, 1.06, 0.95, 0.5),
    "sad": Expression(1.5, 0.97, 0.8, 0.6),
    "angry": Expression(1.3, 0.98, 0.95, 0.4),
    "cry": Expression(1.55, 0.98, 0.8, 0.65, tremolo=0.16),
    "laugh": Expression(1.25, 1.05, 0.95, 0.35, "Ah, ah!"),
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
EMOJI_RUN = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0e\ufe0f\u200d]+")
SENTENCES = re.compile(r"(?<=[.!?;])\s+(?=\w)|\n+")


def speech_segments(text):
    segments = []
    pending = "neutral"
    cursor = 0

    def add_text(fragment):
        nonlocal pending
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


def expressive_audio(audio, expression, sample_rate):
    filters = []
    if expression.pitch != 1.0:
        filters += [f"asetrate={round(sample_rate * expression.pitch)}", f"aresample={sample_rate}", f"atempo={1 / expression.pitch}"]
    if expression.tremolo:
        filters.append(f"tremolo=f=6:d={expression.tremolo}")
    if not filters:
        return audio
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0", "-af", ",".join(filters), "-f", "s16le", "pipe:1"],
        input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout


def synthesize(model, text, output):
    from piper import PiperVoice, SynthesisConfig

    segments = speech_segments(text)
    if not segments:
        return
    voice = PiperVoice.load(model)
    rate = voice.config.sample_rate
    if rate != 22050:
        raise ValueError("The Discord speech model must produce 22050 Hz audio")
    previous_pause = 0.0
    for phrase, mood in segments:
        expression = EXPRESSIONS[mood]
        if expression.reaction:
            phrase = phrase.rstrip(" ,;:")
            phrase += (". " if phrase and phrase[-1] not in ".!?" else " ") + expression.reaction
        config = SynthesisConfig(length_scale=expression.length, volume=expression.volume)
        chunks = [chunk.audio_int16_bytes for chunk in voice.synthesize(phrase, config)]
        if not chunks:
            continue
        silence = bytes(round(rate * expression.pause) * 2)
        audio = expressive_audio(silence.join(chunks), expression, rate)
        if previous_pause:
            output.write(bytes(round(rate * previous_pause) * 2))
        output.write(audio)
        previous_pause = expression.pause


if __name__ == "__main__":
    synthesize(sys.argv[1], sys.stdin.read(), sys.stdout.buffer)
