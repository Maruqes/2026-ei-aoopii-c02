# Plan: natural European Portuguese bot voice

Date: 2026-11-02. Status: implemented.

## Problem

The assistant speaks, but the result sounds unnatural. The current path always uses
Piper `pt_PT-tugão-medium`, a small 22.05 kHz model, and then makes it harder to
understand:

- neutral `length_scale=1.3` (30% slower); sad/sleepy/cry reach 1.5–1.55;
- pitch is changed with `asetrate` + `aresample` + `atempo`, adding resampling
  artifacts;
- volume varies per sentence and long artificial pauses are inserted;
- every sentence is loaded through the same low-quality model.

The result is a slow, wobbly, robotic voice rather than natural PT-PT.

## Decision

Use a neural PT-PT voice from Microsoft Edge TTS (`edge-tts`) as the default:

- default voice `pt-PT-RaquelNeural` (female, friendly, positive);
- `pt-PT-DuarteNeural` (male) available through configuration;
- native SSML rate/pitch/volume for emoji expression, without ffmpeg pitch hacks;
- keep Piper as an automatic offline fallback, with a natural pace (about
  `length_scale=1.05`, no pitch-shift artifacts);
- keep the existing Discord output contract: raw s16le, 22 050 Hz, mono, then
  ffmpeg resamples to 48 kHz stereo Opus in the Go bot.

Edge TTS needs internet access at synthesis time and no API key. If Microsoft is
unreachable or returns no audio, the same reply falls back to the local Piper
model, so voice never blocks the chat reply.

## Changes

1. `discord_bot/speech_synthesis.py`
   - provider selection: `edge` (default) and `piper`;
   - native prosody per expression, concurrent segment synthesis with ordered
     streaming so the first sentence starts early;
   - per-segment Piper fallback after an Edge failure;
   - strip emoji from spoken text (they remain expression cues);
   - restrained Piper prosody: normal pace, no pitch shifting, short pauses.
2. `discord_bot/speech.go`
   - read `ASSISTANT_TTS_PROVIDER` and `ASSISTANT_VOICE`;
   - pass provider/voice/model to Python;
   - keep `ASSISTANT_VOICE_MODEL` for the Piper fallback;
   - include the synthesis stderr tail in errors for diagnosis;
   - clean markdown links, headings, quotes and bullet markers from speech.
3. `discord_bot/Dockerfile`
   - install pinned `edge-tts==7.3.1` beside `piper-tts==1.8.0`.
4. Configuration and documentation
   - `docker-compose.yml`, `.env.example`: new variables;
   - `README.md`, `docs/features/bot-voice.md`: new behaviour and fallback;
   - this plan.
5. Tests
   - provider/voice defaults and overrides;
   - markdown cleaning;
   - Piper path keeps a natural pace and sentence pause.

## Verification

- `cd discord_bot && go test -race ./...` passes.
- `python3 -m unittest test_speech_synthesis` passes (10 tests).
- Edge synthesis produces ~10.8 s of PCM for a 30-word PT-PT sample; the Go
  end-to-end test delivered 273 Opus packets to Discord's send channel.
- `docker compose config -q` passes.
- Rebuild the bot image: `docker compose up -d --build discord-bot`.
- Confirm logs show `voice_ready=true` and no `assistant voice failed` entries.
  A `speech synthesis provider=edge` log entry means the reply fell back to Piper.

## Risks and follow-up

- Edge TTS is an unofficial Microsoft endpoint. The Piper fallback covers outages,
  but a stable deployment may later prefer Azure Speech with the same voices.
- Runtime internet access must remain available in the bot container.
- If the voice should stay fully local, set `ASSISTANT_TTS_PROVIDER=piper`; the
  improved pacing still applies.
